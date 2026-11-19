"""Route connector calls through the normal dispatch policy pipeline."""

import json
from tools.connectors.gateway.merge import partition_calls


def dispatch_connector_call(name, arguments, tool_call_id):
    from tools.connectors.gateway.bridge import run_remote

    partition = partition_calls([{"name": name, "arguments": arguments}])
    entries = run_remote(partition.remote, tool_call_id, availability=None, client_factory=None)
    entry = entries[0]
    return json.dumps({key: value for key, value in entry.items() if key in {"response", "error"}},
                      ensure_ascii=False)
