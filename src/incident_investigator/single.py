"""One investigator conversation using the shared durable call and budget loop."""

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from incident_investigator.runtime import Control, GraphState


def build_single_graph(runner):
    graph = StateGraph(GraphState)

    def begin(state):
        c = Control.model_validate(state["control"])
        runner.start_single(c)
        return {"control": c.model_dump(mode="json")}

    def commit(state):
        c = runner.receive(state)
        task = c.tasks[c.batch[0]]
        if task.status == "succeeded":
            c.hypothesis_revision = c.report_task = task.task_id
            c.status = "completed"
        elif task.status == "failed":
            runner.close(c, "failed", task.failure or "single investigator failed")
        if c.status == "running":
            runner.schedule(c)
        return runner.checkpoint(c)

    def route(state):
        c = state["control"]
        if c["status"] != "running" or not c["dispatch"]:
            return END
        return [Send("worker", {"call": c["calls"][key]}) for key in c["dispatch"]]

    graph.add_node("begin", begin)
    graph.add_node("commit", commit)
    graph.add_node("worker", runner.execute)
    graph.add_edge(START, "begin")
    graph.add_edge("begin", "commit")
    graph.add_edge("worker", "commit")
    graph.add_conditional_edges("commit", route, ["worker", END])
    return graph
