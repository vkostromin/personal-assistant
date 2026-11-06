"""Minimal in-memory stand-in for ``google.cloud.firestore.AsyncClient``.

Supports exactly the subset used by ``src.state.FirestoreStore`` (documents,
equality queries, dotted-path updates, and transactions driven by the *real*
``firestore.async_transactional`` decorator), so the production store code is
exercised by the tests.
"""

import copy
import itertools
from datetime import datetime, timezone

from google.api_core import exceptions

_ids = itertools.count(1)


def _normalize(value):
    # Firestore returns timestamps as tz-aware UTC datetimes
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


class FakeSnapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self._data = copy.deepcopy(data)

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, client, collection, doc_id):
        self._client = client
        self._collection = collection
        self.id = doc_id

    @property
    def _docs(self):
        return self._client.data.setdefault(self._collection, {})

    async def get(self, transaction=None):
        return FakeSnapshot(self, self._docs.get(self.id))

    def _set(self, data, merge=False):
        data = _normalize(copy.deepcopy(data))
        if merge and self.id in self._docs:
            self._docs[self.id].update(data)
        else:
            self._docs[self.id] = data

    def _update(self, updates):
        if self.id not in self._docs:
            raise exceptions.NotFound(f"{self._collection}/{self.id}")
        doc = self._docs[self.id]
        for path, value in updates.items():
            target = doc
            *parents, leaf = path.split(".")
            for p in parents:
                target = target.setdefault(p, {})
            target[leaf] = _normalize(copy.deepcopy(value))

    def _create(self, data):
        if self.id in self._docs:
            raise exceptions.AlreadyExists(f"{self._collection}/{self.id}")
        self._set(data)

    async def set(self, data, merge=False):
        self._set(data, merge)

    async def update(self, updates):
        self._update(updates)

    async def create(self, data):
        self._create(data)

    async def delete(self):
        self._docs.pop(self.id, None)


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a is not None and a < b,
    ">": lambda a, b: a is not None and a > b,
}


class FakeQuery:
    def __init__(self, client, collection, filters=()):
        self._client = client
        self._collection = collection
        self._filters = list(filters)

    def where(self, *args, filter=None):
        f = filter
        return FakeQuery(self._client, self._collection, self._filters + [(f.field_path, f.op_string, f.value)])

    async def stream(self, transaction=None):
        docs = self._client.data.get(self._collection, {})
        for doc_id, data in list(docs.items()):
            if all(_OPS[op](data.get(field), value) for field, op, value in self._filters):
                yield FakeSnapshot(FakeDocRef(self._client, self._collection, doc_id), data)


class FakeCollection(FakeQuery):
    def __init__(self, client, name):
        super().__init__(client, name)

    def document(self, doc_id=None):
        return FakeDocRef(self._client, self._collection, doc_id or f"auto{next(_ids)}")


class FakeTransaction:
    """Buffers writes until commit, like a real Firestore transaction."""

    def __init__(self):
        self._id = None
        self._read_only = False
        self._max_attempts = 5
        self._writes = []
        self.committed = False
        self.rolled_back = False

    def _clean_up(self):
        self._writes = []
        self._id = None

    async def _begin(self, retry_id=None):
        self._id = b"txn"

    async def _commit(self):
        for fn in self._writes:
            fn()
        self._writes = []
        self.committed = True
        return []

    async def _rollback(self):
        self._writes = []
        self.rolled_back = True

    def set(self, ref, data, merge=False):
        self._writes.append(lambda: ref._set(data, merge))

    def update(self, ref, data):
        self._writes.append(lambda: ref._update(data))

    def create(self, ref, data):
        self._writes.append(lambda: ref._create(data))

    def delete(self, ref):
        self._writes.append(lambda: ref._docs.pop(ref.id, None))


class FakeAsyncClient:
    def __init__(self):
        self.data: dict[str, dict[str, dict]] = {}

    def collection(self, name):
        return FakeCollection(self, name)

    def transaction(self):
        return FakeTransaction()
