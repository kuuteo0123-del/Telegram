@echo off
setlocal
python -m venv .venv312
call .venv312\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -c "print('Environment ready')"

# setup.bat
