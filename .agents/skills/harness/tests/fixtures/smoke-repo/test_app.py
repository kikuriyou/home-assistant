import unittest

import app


class AppTest(unittest.TestCase):
    def test_value(self):
        self.assertEqual(1, app.value())


if __name__ == "__main__":
    unittest.main()
