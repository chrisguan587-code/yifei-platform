from pathlib import Path
import plistlib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ConceptMembershipLaunchdContractTest(unittest.TestCase):
    def test_platform_owns_weekly_concept_publication(self):
        path = ROOT / (
            "ops/launchd/"
            "com.yplus.yifei-platform.concept-membership.plist"
        )
        payload = plistlib.loads(path.read_bytes())
        arguments = payload["ProgramArguments"]
        schedule = payload["StartCalendarInterval"]

        self.assertEqual(
            "com.yplus.yifei-platform.concept-membership",
            payload["Label"],
        )
        self.assertIn("yifei-platform-publish-concepts", arguments[0])
        self.assertIn(
            "/Users/y-plus/projects/yifei/data/shared/concepts",
            arguments,
        )
        self.assertEqual(
            {"Weekday": 6, "Hour": 18, "Minute": 30}, schedule
        )


if __name__ == "__main__":
    unittest.main()
