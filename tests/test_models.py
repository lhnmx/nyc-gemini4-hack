import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.models import ProjectState
from tests.test_planner import make_plan


class GenerationContractTests(unittest.TestCase):
    def test_old_projects_load_as_continuous_chapters_with_unknown_model_provenance(self):
        legacy_plan = make_plan().model_dump(exclude={"steps": {"__all__": {"visual_parent"}}})
        project = ProjectState.model_validate({
            "project_id": "paper", "goal": "Fold paper", "plan": legacy_plan,
            "media": {"1": {"step_number": 1, "status": "ready"}},
        })
        self.assertEqual([step.visual_parent for step in project.plan.steps], [-1, -1])
        self.assertIsNone(project.media[1].video_model)
        self.assertIsNone(project.media[1].voice_model)
        self.assertIsNone(project.media_progress)
        self.assertEqual(project.activity, [])

    @patch.dict(os.environ, clear=True)
    def test_shot_concurrency_and_optional_catalog_profile_settings(self):
        defaults = Settings(_env_file=None)
        self.assertEqual(defaults.max_parallel_shots, 2)
        self.assertEqual(defaults.shopify_agent_profile_url, "")
        profile = "https://catalog.example/profile.json"
        for limit in range(1, 5):
            with patch.dict(os.environ, MAX_PARALLEL_SHOTS=str(limit), SHOPIFY_AGENT_PROFILE_URL=profile):
                settings = Settings(_env_file=None)
                self.assertEqual(settings.max_parallel_shots, limit)
                self.assertEqual(settings.shopify_agent_profile_url, profile)
        for invalid in (0, 5):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                Settings(_env_file=None, max_parallel_shots=invalid)
