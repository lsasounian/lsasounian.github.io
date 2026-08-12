from typing import Annotated, Optional, TypedDict

from langgraph.graph.message import add_messages


class MediatorState(TypedDict):
    messages: Annotated[list, add_messages]
    raw_query: Optional[str]
    refined_query: Optional[str]
    active_skill: Optional[str]
    skill_instructions: Optional[str]
    skill_kb_ids: list[str]
    bound_tool_names: list[str]
