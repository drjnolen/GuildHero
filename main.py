from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parent
BOT_DIR = PROJECT_DIR / "CityLedger"

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from bot import main


if __name__ == "__main__":
    main()
