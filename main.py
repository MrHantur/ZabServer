# main.py (в корне проекта)
import uvicorn
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, если нужно
project_root = Path(__file__).parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.config import IS_DEV

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="localhost", port=1717, reload=IS_DEV)