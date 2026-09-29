import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))
from district_works.contracts import AccessCommitment, WorkTask

task = WorkTask("W-6", "SEG-2", ("W-3",), 2)
access = AccessCommitment("SEG-2", 1.5, True)
print(json.dumps({"task": task.task_id, "dependencies": len(task.depends_on), "access_width": access.minimum_width_m}, ensure_ascii=False))
