"""A stand-in for Supabase/PostgREST that models the semantics the queue relies on.

It is deliberately modelled on how Postgres actually executes one UPDATE statement at
READ COMMITTED, because that is the guarantee the claim protocol is built on:

  * A single UPDATE is one statement in one transaction. Here that is one critical
    section under a table lock.
  * When two UPDATEs contend for a row, the second waits for the first to commit and
    then RE-EVALUATES its WHERE clause against the committed new version of the row
    (EvalPlanQual). Here the predicate is evaluated INSIDE the lock, and a random sleep
    is injected BEFORE taking the lock so the interleaving is genuinely raced rather
    than accidentally serialised by luck.
  * SELECT sees a snapshot and takes no locks -- so candidate lists can be stale, which
    is exactly the condition the claim protocol has to survive.
  * A partial unique index rejects a conflicting INSERT with SQLSTATE 23505.

What this proves: the claim/fence/defer/sweep protocol in video_jobs.py is correct
GIVEN those semantics. What it does not prove: that Supabase provides them. That part
rests on the documented Read Committed behaviour and on PostgREST issuing one statement
per request, and is checked separately against the real database.
"""
import copy
import random
import threading
import time
import uuid


class FakeAPIError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


def _cmp_key(value):
    return "" if value is None else str(value)


class _Query:
    def __init__(self, db, table, op, payload=None, count=None):
        self.db = db
        self.table = table
        self.op = op
        self.payload = payload
        self.count_mode = count
        self.filters = []          # (kind, column, value)
        self.order_by = None
        self.order_desc = False
        self._limit = None

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def lt(self, column, value):
        self.filters.append(("lt", column, value))
        return self

    def gt(self, column, value):
        self.filters.append(("gt", column, value))
        return self

    def lte(self, column, value):
        self.filters.append(("lte", column, value))
        return self

    def in_(self, column, values):
        self.filters.append(("in", column, list(values)))
        return self

    def order(self, column, desc=False):
        self.order_by = column
        self.order_desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _matches(self, row):
        for kind, column, value in self.filters:
            current = row.get(column)
            if kind == "eq":
                if current != value:
                    return False
            elif kind == "in":
                if current not in value:
                    return False
            elif kind == "lt":
                if current is None or _cmp_key(current) >= _cmp_key(value):
                    return False
            elif kind == "lte":
                if current is None or _cmp_key(current) > _cmp_key(value):
                    return False
            elif kind == "gt":
                if current is None or _cmp_key(current) <= _cmp_key(value):
                    return False
        return True

    def _select_rows(self, rows):
        out = [copy.deepcopy(r) for r in rows if self._matches(r)]
        if self.order_by:
            out.sort(key=lambda r: _cmp_key(r.get(self.order_by)), reverse=self.order_desc)
        total = len(out)
        if self._limit is not None:
            out = out[: self._limit]
        return out, total

    def execute(self):
        self.db.calls[self.op] = self.db.calls.get(self.op, 0) + 1

        if self.op == "select":
            # No lock: a SELECT sees a snapshot and can be stale the instant it returns.
            rows, total = self._select_rows(self.db.rows)
            return _Result(rows, total if self.count_mode == "exact" else None)

        # Every write below is one statement. The sleep is OUTSIDE the lock so threads
        # genuinely interleave; the predicate is evaluated INSIDE it, which is the
        # EvalPlanQual re-check that makes a conditional UPDATE a valid compare-and-swap.
        time.sleep(random.random() * 0.004)
        with self.db.lock:
            if self.op == "insert":
                row = dict(self.payload)
                row.setdefault("id", str(uuid.uuid4()))
                for key in ("claimed_by", "claimed_at", "heartbeat_at", "started_at",
                            "finished_at", "duration_seconds", "result_url",
                            "degradations", "error"):
                    row.setdefault(key, None)
                self.db._enforce_unique_active(row)
                self.db.rows.append(row)
                return _Result([copy.deepcopy(row)])

            if self.op == "update":
                updated = []
                for row in self.db.rows:
                    if self._matches(row):
                        row.update(copy.deepcopy(self.payload))
                        updated.append(copy.deepcopy(row))
                return _Result(updated)

            if self.op == "delete":
                kept, removed = [], []
                for row in self.db.rows:
                    (removed if self._matches(row) else kept).append(row)
                self.db.rows = kept
                return _Result([copy.deepcopy(r) for r in removed])

        raise AssertionError("unknown op %r" % self.op)


class _Table:
    def __init__(self, db, name):
        self.db = db
        self.name = name

    def select(self, *columns, **kwargs):
        return _Query(self.db, self.name, "select", count=kwargs.get("count"))

    def insert(self, payload):
        return _Query(self.db, self.name, "insert", payload=payload)

    def update(self, payload):
        return _Query(self.db, self.name, "update", payload=payload)

    def delete(self):
        return _Query(self.db, self.name, "delete")


class RlsFakeDB(object):
    """A database our key can SELECT from but not INSERT into.

    This is what Supabase actually does with `alter table ... enable row level
    security` and no policies, for a key that is subject to RLS (i.e. the anon key
    rather than the service key): default-deny FILTERS ROWS, it does not revoke the
    privilege. So a SELECT succeeds and returns an empty array -- HTTP 200, no error --
    while an INSERT is refused with 42501. Any probe that only reads cannot tell this
    apart from an empty table."""

    def __init__(self):
        self.inner = FakeDB()

    def table(self, name):
        return _RlsTable(self.inner, name)


class _RlsTable(_Table):
    def insert(self, payload):
        raise FakeAPIError(
            'new row violates row-level security policy for table "video_jobs"',
            code="42501")

    def select(self, *columns, **kwargs):
        # Succeeds, and returns nothing, because every row is filtered out.
        return _Query(FakeDB(), self.name, "select", count=kwargs.get("count"))


class FakeDB:
    def __init__(self):
        self.rows = []
        self.lock = threading.RLock()
        self.calls = {}

    def table(self, name):
        return _Table(self, name)

    def _enforce_unique_active(self, new_row):
        """The partial unique index from the migration:
        UNIQUE (listing_id, job_type) WHERE status IN ('queued','running')."""
        if new_row.get("status") not in ("queued", "running"):
            return
        for row in self.rows:
            if (row.get("listing_id") == new_row.get("listing_id")
                    and row.get("job_type") == new_row.get("job_type")
                    and row.get("status") in ("queued", "running")):
                raise FakeAPIError(
                    'duplicate key value violates unique constraint '
                    '"video_jobs_one_active_per_listing_type"', code="23505")


def make_db_execute(db):
    """Mimics main.db_execute's signature, including the retry/idempotent knobs."""
    def db_execute(build, attempts=3, what="query", idempotent=True):
        return build(db).execute()
    return db_execute
