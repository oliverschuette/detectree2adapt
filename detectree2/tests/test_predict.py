import unittest


class TestCase(unittest.TestCase):

    def test_f1_cal(self):
        from detectree2.models.evaluation import f1_cal

        precision = 0.5
        recall = 1 / 9
        self.assertAlmostEqual(f1_cal(precision, recall), 0.1818, places=4)

    def test_prec_recall_func(self):
        from detectree2.models.evaluation import prec_recall

        tps = 1
        fps = 1
        fns = 8
        tns = 90  # noqa:F841
        prec, recall = prec_recall(tps, fps, fns)
        self.assertEqual(prec, 0.5)
        self.assertEqual(recall, 1 / 9)


suite = unittest.TestLoader().loadTestsFromTestCase(TestCase)
