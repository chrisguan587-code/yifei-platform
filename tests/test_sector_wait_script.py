import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class SectorWaitScriptTest(unittest.TestCase):
    def test_pending_market_data_waits_before_both_publications(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'scripts').mkdir();(root/'.venv/bin').mkdir(parents=True)
            script=root/'scripts/run_sector_market_daily.sh'
            script.write_text((Path(__file__).parents[1]/'scripts/run_sector_market_daily.sh').read_text())
            python=root/'.venv/bin/python'
            python.write_text('''#!/bin/sh
cat >/dev/null
n=0
if [ -f "$0.count" ]; then n=$(cat "$0.count"); fi
n=$((n+1)); echo "$n" > "$0.count"
if [ "$n" -eq 2 ]; then exit 3; fi
exit 0
''')
            python.chmod(0o755)
            publisher=root/'.venv/bin/yifei-platform-supplemental'
            publisher.write_text('#!/bin/sh\necho "$1" >> "$0.calls"\n')
            publisher.chmod(0o755)
            sleep=root/'.venv/bin/sleep'
            sleep.write_text('#!/bin/sh\necho waited >> "$0.calls"\n')
            sleep.chmod(0o755)
            run=subprocess.run(['/bin/sh',str(script),'market','supplemental','readiness'],
                env={**os.environ,'PATH':str(python.parent)+':/bin:/usr/bin'},
                capture_output=True,text=True,timeout=5)
            self.assertEqual(0,run.returncode,run.stderr)
            self.assertEqual('waited\n',sleep.with_name('sleep.calls').read_text())
            self.assertEqual(['publish-sector-market-daily','publish-sector-market-daily-v2'],
                publisher.with_name(publisher.name+'.calls').read_text().splitlines())
