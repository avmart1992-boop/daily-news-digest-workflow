"""Worker entrypoint: registers all workflows in worker.workflows."""
import asyncio

import mistralai.workflows as workflows
from mistralai.client import Mistral


async def main() -> None:
    client = Mistral()
    workflow_classes = workflows.discover_all_workflows_in_package("worker.workflows")
    await client.workflows.run_worker(workflow_classes)


if __name__ == "__main__":
    asyncio.run(main())
