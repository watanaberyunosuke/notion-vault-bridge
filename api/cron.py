from api.sync import handler as SyncHandler


class handler(SyncHandler):
    def do_GET(self):
        self.dispatch_bridge("cron")
