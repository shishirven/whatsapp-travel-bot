class MemoryGateway:
    """Records sends. No network. Each send gets its own id."""

    def __init__(self):
        self.sent = []

    def send_text(self, to, body):
        self.sent.append(("text", to, body))
        return f"text-{len(self.sent)}"

    def send_template(self, to, name, params):
        self.sent.append(("template", to, name, list(params)))
        return f"template-{len(self.sent)}"


class FakeCalendar:
    def __init__(self, busy=None):
        self.busy = list(busy or [])
        self.events = []

    def busy_intervals(self, start, end):
        return list(self.busy)

    def create_event(self, start, end, summary, description):
        self.events.append(
            {"start": start, "end": end, "summary": summary, "description": description}
        )
        return "evt-1"
