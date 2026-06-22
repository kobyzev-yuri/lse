import unittest

from services.game5m_ml_card_advice import (
    entry_e3_advice_from_d5,
    hold_h3_advice_from_shadow,
)


class TestGame5mMlCardAdvice(unittest.TestCase):
    def test_entry_e3_would_enter(self):
        a = entry_e3_advice_from_d5(
            {"entry_e3_signal_status": "ok", "catboost_entry_proba_good_e3": 0.62}
        )
        self.assertEqual(a["label_ru"], "за вход")
        self.assertTrue(a["would_enter"])

    def test_entry_e3_against(self):
        a = entry_e3_advice_from_d5(
            {"entry_e3_signal_status": "ok", "catboost_entry_proba_good_e3": 0.41}
        )
        self.assertEqual(a["label_ru"], "против входа")
        self.assertFalse(a["would_enter"])

    def test_hold_h3_defer(self):
        a = hold_h3_advice_from_shadow(
            {"status": "ok", "hold_quality_proba": 0.71, "would_defer_exit": True, "tau_hold": 0.55}
        )
        self.assertEqual(a["label_ru"], "за удержание")

    def test_hold_h3_exit(self):
        a = hold_h3_advice_from_shadow(
            {"status": "ok", "hold_quality_proba": 0.32, "would_defer_exit": False, "tau_hold": 0.55}
        )
        self.assertEqual(a["label_ru"], "за выход")


if __name__ == "__main__":
    unittest.main()
