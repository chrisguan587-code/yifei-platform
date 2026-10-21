from pathlib import Path
import subprocess
import unittest


class CenterPublicationTriggerTest(unittest.TestCase):
    def test_center_publication_follows_successful_market_publication(self):
        script=(Path(__file__).resolve().parents[1]/'scripts/run_daily_market.sh')
        subprocess.run(['/bin/sh','-n',str(script)],check=True)
        text=script.read_text()
        self.assertIn('yifei_platform.center_facts_cli',text)
        self.assertLess(text.index('--published-at "$PUBLISHED_AT"; then'),text.index('yifei_platform.center_facts_cli'))
        self.assertIn('exec env PYTHONPATH=',text)
        self.assertIn('consolidation_centers',text)
        self.assertNotIn('yifei-shortline',text)
