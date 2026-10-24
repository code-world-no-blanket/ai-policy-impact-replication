"""Public, repository-linked Projects v2 and complete Status item counts."""

from collections import Counter

from ai_policy_impact.github import APIError, GitHubClient

LIST_QUERY = """query($owner:String!,$name:String!,$cursor:String) {
  repository(owner:$owner,name:$name) { projectsV2(first:100,after:$cursor) {
    nodes { id number title url public closed }
    pageInfo { hasNextPage endCursor }
  } }
}"""
FIELDS_QUERY = """query($id:ID!,$cursor:String) {
  node(id:$id) { ... on ProjectV2 { fields(first:100,after:$cursor) {
    nodes { ... on ProjectV2SingleSelectField { id name options { id name } } }
    pageInfo { hasNextPage endCursor }
  } } }
}"""
ITEMS_QUERY = """query($id:ID!,$cursor:String) {
  node(id:$id) { ... on ProjectV2 { items(first:100,after:$cursor,archivedStates:[ARCHIVED,NOT_ARCHIVED]) {
    totalCount nodes { id type isArchived
      fieldValueByName(name:"Status") {
        ... on ProjectV2ItemFieldSingleSelectValue { optionId name }
      }
    } pageInfo { hasNextPage endCursor }
  } } }
}"""


def pages(client: GitHubClient, query: str, variables: dict, parent: str, field: str):
    cursor = None
    seen = set()
    for _ in range(100):
        data = client.graphql(query, {**variables, "cursor": cursor})
        root = data.get(parent)
        if not root or field not in root:
            raise APIError(200, f"Missing GraphQL {parent}.{field}")
        connection = root[field]
        yield connection
        info = connection["pageInfo"]
        if not info["hasNextPage"]:
            return
        cursor = info["endCursor"]
        if not cursor or cursor in seen:
            raise APIError(200, "Invalid or repeated pagination cursor")
        seen.add(cursor)
    raise APIError(200, "Pagination safety limit reached; counts are incomplete")


def summarize_items(options: list[dict], items: list[dict]) -> list[dict]:
    """Include empty columns, unassigned items, drafts, and archived counts."""
    counts = Counter()
    names = {option["id"]: option["name"] for option in options}
    names[None] = "(no Status)"
    for item in items:
        value = item.get("fieldValueByName")
        option = value["optionId"] if value else None
        if option not in names:
            raise APIError(
                200, "Status options changed during collection; recollect this board"
            )
        counts[(option, bool(item["isArchived"]), item["type"])] += 1
    return [
        {
            "option_id": key,
            "status_name": name,
            "active_items": sum(
                n for (o, archived, _), n in counts.items() if o == key and not archived
            ),
            "archived_items": sum(
                n for (o, archived, _), n in counts.items() if o == key and archived
            ),
            "by_type": {
                kind: sum(
                    n for (o, _, t), n in counts.items() if o == key and t == kind
                )
                for kind in sorted({i["type"] for i in items})
            },
        }
        for key, name in names.items()
    ]


def collect_projects(client: GitHubClient, full_name: str) -> dict:
    owner, name = full_name.split("/")
    result = {
        "status": "complete",
        "scope": "public_repository_linked_projects_v2",
        "projects": [],
        "private_projects_skipped": 0,
    }
    try:
        for page in pages(
            client,
            LIST_QUERY,
            {"owner": owner, "name": name},
            "repository",
            "projectsV2",
        ):
            for project in page["nodes"]:
                if not project["public"]:
                    result["private_projects_skipped"] += 1
                    continue
                board = {
                    **project,
                    "status": "complete",
                    "count_scope": "whole_project_all_item_types",
                }
                result["projects"].append(board)
                try:
                    fields = [
                        f
                        for p in pages(
                            client,
                            FIELDS_QUERY,
                            {"id": project["id"]},
                            "node",
                            "fields",
                        )
                        for f in p["nodes"]
                        if f
                    ]
                    status_field = next(
                        (f for f in fields if f.get("name") == "Status"), None
                    )
                    if status_field is None:
                        board["status"] = "status_field_not_found"
                        continue
                    items = []
                    totals = set()
                    for p in pages(
                        client, ITEMS_QUERY, {"id": project["id"]}, "node", "items"
                    ):
                        items.extend(p["nodes"])
                        totals.add(p["totalCount"])
                    if totals != {len(items)} or len({i["id"] for i in items}) != len(
                        items
                    ):
                        raise APIError(
                            200, "Item count changed during pagination; counts withheld"
                        )
                    board["columns"] = summarize_items(status_field["options"], items)
                    board["total_items"] = len(items)
                except APIError as error:
                    board.update(status="unavailable", error=str(error))
        if any(p["status"] != "complete" for p in result["projects"]):
            result["status"] = "partial"
    except APIError as error:
        result.update(status="unavailable", error=str(error))
    return result
