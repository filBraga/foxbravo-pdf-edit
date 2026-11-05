# Foxbravo PDF Edit

A small, free desktop PDF editor for Windows, written in Python with Tkinter and [PyMuPDF](https://pymupdf.readthedocs.io/).

## Features

- **Edit existing text**: hover over a line, click it, and type. Only the part you changed is rewritten.
- **Find & replace** across the whole document (Ctrl+H)
- **Highlight**, **freehand drawing**, **text boxes** and **sticky notes**
- **Sign**: draw your signature or load an image of it, then place it on the page (it can be remembered for next time)
- **Fill in forms**: text fields, checkboxes, radio buttons and drop-down lists
- **Merge**: add PDFs at the end, or insert one after the current page
- **Split**: extract chosen pages (e.g. `1-3, 5`) or save one PDF per page
- **Pages**: rotate, move up/down, delete
- **Undo / redo** for every change; nothing is written to disk until you save
- Opens password-protected PDFs, and keeps the password when saving

## How text editing works

PDFs usually embed only the letters a document actually uses. When you change text, the app picks a font in this order:

1. **The document's own font**, if every letter you typed already appears in the document with that font
2. **The same font installed on your computer** (e.g. Calibri or Arial), found by name
3. **A close standard font** (Helvetica, Times or Courier)
4. **A similar installed font** (e.g. Arial or Times New Roman) for letters the standard fonts don't have

The status bar tells you which one was used. Before erasing anything, the app tests the change on a copy of the page. If any other text would be damaged or would move, it cancels the edit and tells you.

**Limits:** scanned PDFs (pictures of text) and rotated or vertical text can't be edited. Text that becomes much longer may run into what follows it. For that case, the edit dialog has a *Squeeze longer text to fit* option.

## Build the .exe (Windows)

1. Install **Python 3.10+** from [python.org](https://www.python.org/downloads/). During setup, tick *Add python.exe to PATH*.
2. Double-click **`build.bat`**.

The script creates a private environment in `.venv`, installs the pinned packages from `requirements.txt` plus PyInstaller, and builds `FoxbravoPDFEdit.exe` in this folder. The `.exe` is self-contained, so you can copy it anywhere.

Windows SmartScreen may warn the first time you run it, because the file isn't code-signed. Click *More info → Run anyway*.

## Run from source

```bash
pip install -r requirements.txt
python foxbravo_pdf_edit.py [file.pdf]
```

## Where things are stored

`%APPDATA%\FoxbravoPDFEdit\` holds your remembered signature (`signature.png`) and a cache of installed fonts (`font-index.json`). Delete the folder to reset.

## License

This project uses PyMuPDF, which is licensed under **AGPL-3.0** (or a commercial license from Artifex). If you publish or distribute this app, release it under AGPL-3.0 too, or buy a commercial PyMuPDF license.
