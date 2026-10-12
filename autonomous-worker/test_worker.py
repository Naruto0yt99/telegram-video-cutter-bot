import os
import tempfile
import unittest
from pathlib import Path

# Isolate the database for every test before importing the module.
_temp = tempfile.TemporaryDirectory()
os.environ["WORKER_DB"] = str(Path(_temp.name) / "test.sqlite3")

import worker  # noqa: E402


class WorkerQueueTests(unittest.TestCase):
    def test_add_and_list_task(self):
        task_id = worker.add_task("Test task", "Verify queue persistence")
        rows = worker.list_tasks()
        self.assertTrue(any(row["id"] == task_id and row["status"] == "queued" for row in rows))

    def test_checkpoint_existing_task(self):
        task_id = worker.add_task("Checkpoint task", "Save a progress marker")
        self.assertTrue(worker.set_checkpoint(task_id, "step 1 passed"))
        row = next(row for row in worker.list_tasks() if row["id"] == task_id)
        self.assertEqual(row["checkpoint"], "step 1 passed")

    def test_checkpoint_unknown_task(self):
        self.assertFalse(worker.set_checkpoint(999999, "not found"))


if __name__ == "__main__":
    unittest.main()
