class UpdateController:
    def __init__(self, stack_manager):
        self.stack_manager = stack_manager

    def status(self):
        return self.stack_manager.get_stack_status()

    def check_online(self):
        return self.stack_manager.check_updates()

    def synchronize(self):
        return self.stack_manager.sync_runtime_from_geniex()

    def update_geniex(self):
        return self.stack_manager.update_geniex_cli()
