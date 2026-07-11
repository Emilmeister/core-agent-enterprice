from .errors import CoreError


class MemoryAuthoringClient:
    def __init__(self, mcp):
        self.mcp = mcp

    def remember(self, *, title, content, namespace):
        search = self.mcp.call(
            "memory.search", {"query": title, "namespace": namespace}
        )
        results = search.get("results", [])
        try:
            if results and results[0].get("same_topic"):
                match = results[0]
                return self.mcp.call(
                    "memory.update",
                    {
                        "memory_id": match["memory_id"],
                        "content": content,
                        "expected_file_revision": match["revision"],
                    },
                )
            return self.mcp.call(
                "memory.create",
                {"title": title, "content": content, "namespace": namespace},
            )
        except CoreError as error:
            if error.code == "MEMORY_FILE_TOO_LARGE":
                return {
                    "status": "split_recommended",
                    "next_tool": "memory.split",
                    "committed": False,
                    "details": error.data,
                }
            raise
