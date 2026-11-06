"""In-memory Notion substitute for offline examples; never sends network requests."""
import copy
import uuid

from bridge import BridgeError


class FakeNotion:
    def __init__(self):
        self.pages = {}
        self.creates = 0
        self.updates = 0
        self.fail_after_create = False
        self.on_get = None
        self.on_create = None

    def children(self, parent):
        return list(self.pages)

    def get(self, pid):
        if self.on_get:
            callback, self.on_get = self.on_get, None
            callback()
        return copy.deepcopy(self.pages[pid])

    def create(self, parent, title, body):
        self.creates += 1
        pid = str(uuid.uuid4())
        self.pages[pid] = {"id": pid, "title": title, "body": body}
        if self.on_create:
            self.on_create()
        if self.fail_after_create:
            raise BridgeError("Simulated lost response after server created page")
        return pid

    def update(self, pid, old_body, new_body, title=None):
        if self.pages[pid]["body"] != old_body:
            raise BridgeError("Remote changed")
        self.updates += 1
        self.pages[pid]["body"] = new_body
        if title is not None:
            self.pages[pid]["title"] = title
