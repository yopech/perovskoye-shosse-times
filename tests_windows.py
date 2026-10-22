import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import windows_backend as win


class WindowsBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.pdf=self.root/'issue.pdf'
        self.pdf.write_bytes(b'Only a mocked print input')
        self.cfg={'printer':'Office "A"; $(fake)','windows':{}}
        self.day='2026-10-22'

    def tearDown(self):
        self.tmp.cleanup()

    def test_sumatra_uses_argument_list_and_one_copy(self):
        with patch.object(win,'STATE',self.root),patch.object(win,'sumatra_path',return_value='SumatraPDF.exe'),\
            patch.object(win,'jobs',return_value=[{'ID':7,'DocumentName':'perovskoye-times-2026-10-22.pdf'}]),\
            patch.object(win.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            result=win.submit(self.cfg,self.pdf,self.day)
            self.assertEqual(result['state'],'submitted')
            self.assertEqual(result['job_id'],'7')
            args=run.call_args.args[0]
            self.assertEqual(args[2],self.cfg['printer'])
            self.assertIn('paper=A4,simplex,1x',args)
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_no_queue_id_can_mean_already_spooled(self):
        with patch.object(win,'STATE',self.root),patch.object(win,'sumatra_path',return_value='SumatraPDF.exe'),\
            patch.object(win,'jobs',return_value=[]),patch.object(win.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            result=win.submit(self.cfg,self.pdf,self.day)
            self.assertEqual(result['state'],'submitted')
            self.assertNotIn('job_id',result)

    def test_failed_print_is_not_success(self):
        with patch.object(win,'STATE',self.root),patch.object(win,'sumatra_path',return_value='SumatraPDF.exe'),\
            patch.object(win.subprocess,'run',return_value=SimpleNamespace(returncode=4)):
            self.assertEqual(win.submit(self.cfg,self.pdf,self.day)['state'],'uncertain_or_failed')

    def test_cutoff_does_not_cancel_recycled_job_id(self):
        ledger={'job_id':'7','document':'ours.pdf'}
        with patch.object(win,'jobs',return_value=[{'ID':7,'DocumentName':'someone-else.pdf'}]),patch.object(win,'powershell') as ps:
            self.assertFalse(win.cancel_pending(self.cfg,ledger))
            ps.assert_not_called()

    def test_cancel_uses_env_not_interpolated_printer_name(self):
        ledger={'job_id':'7','document':'ours.pdf'}
        with patch.object(win,'jobs',return_value=[{'ID':7,'DocumentName':'ours.pdf'}]),patch.object(win,'powershell') as ps:
            self.assertTrue(win.cancel_pending(self.cfg,ledger))
            script,values=ps.call_args.args
            self.assertNotIn(self.cfg['printer'],script)
            self.assertEqual(values['GAZETTE_PRINTER'],self.cfg['printer'])

    def test_schedule_is_interactive_limited_and_quotes_paths(self):
        with patch.object(win,'powershell') as ps:
            win.install_schedule()
            script,values=ps.call_args.args
            self.assertIn('-LogonType Interactive -RunLevel Limited',script)
            self.assertNotIn(str(win.ROOT),script)
            self.assertEqual(values['GAZETTE_ROOT'],str(win.ROOT))


if __name__=='__main__':
    unittest.main()
