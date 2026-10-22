"""実測データに依存せず、既知の人工曲線で計算・単独実行を検証する。"""
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import lp_calculation as lp
import numpy as np

ROOT = Path(__file__).resolve().parent


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.config = lp.load_config(ROOT / 'settings.json')
        self.result = lp.calculate(ROOT / self.config['input'], self.config)

    def test_known_fits_potentials_temperature_and_density(self):
        result = self.result
        self.assertAlmostEqual(result.fits['ion'].slope, 4e-8, delta=1e-20)
        self.assertAlmostEqual(result.fits['ion'].intercept, -1.5e-7, delta=1e-20)
        self.assertAlmostEqual(result.fits['retarding'].slope, .7, places=12)
        self.assertAlmostEqual(result.fits['saturation'].slope, .1, places=12)
        for name, expected in [('floating_potential_V', .25), ('derivative_peak_voltage_V', 5.75),
                               ('plasma_potential_V', 6.), ('electron_temperature_eV', 1/.7)]:
            self.assertAlmostEqual(result.metrics[name], expected, places=11)
        k, charge, mass, radius = 1.38e-23, 1.6e-19, 9.10939e-31, .02
        temperature = charge / (k * .7)
        speed = math.sqrt(8 * k * temperature / (math.pi * mass))
        expected = 4 * math.exp(-13.8) / (charge * 4 * math.pi * radius**2 * speed) / 1e6
        self.assertTrue(math.isclose(result.metrics['electron_density_cm3'], expected, rel_tol=1e-12))

    def test_zero_residual_and_saved_legacy_ion_selection_are_ignored(self):
        negative = self.result.voltage < 0
        np.testing.assert_array_equal(self.result.trace.electron_current[negative], 0)
        self.assertTrue(np.all(np.isnan(self.result.trace.log_current[negative])))
        config = self.config | {'ion_fit_rows': [27, 30], 'ion_slope_A_per_V': 99}
        again = lp.calculate(ROOT / config['input'], config)
        self.assertEqual(again.metrics, self.result.metrics)

    def test_cli_works_from_another_directory_and_writes_results(self):
        temporary_parent = ROOT / '.tools'
        temporary_parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='lp analysis ', dir=temporary_parent) as temporary:
            out = Path(temporary) / 'results'
            result = subprocess.run([sys.executable, '-I', '-X', 'utf8', str(ROOT / 'lp_calculation.py'),
                                     '--config', str(ROOT / 'settings.json'), '--output-dir', str(out)],
                                    cwd=temporary, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
            self.assertEqual(summary['metrics'], self.result.metrics)
            self.assertTrue((out / 'analysis.csv').exists())


if __name__ == '__main__':
    unittest.main()
