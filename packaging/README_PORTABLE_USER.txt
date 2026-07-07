Open Accounting for Windows
===========================

Version: v0.1.0
Built from: https://github.com/ai-coding-lab-au/open-accounting/tree/v0.1.0

How to start
------------

Open the OpenAccounting folder and double-click:

  OpenAccounting.exe

First run: Windows SmartScreen may show "Windows protected your PC" because
this build is not code-signed yet. Click "More info", then "Run anyway".
If you prefer not to, you can always run from source instead (see the
repository link below).

The app starts a local server on your own computer and opens your browser at:

  http://127.0.0.1:8787

Keep the whole OpenAccounting folder together. Do not move OpenAccounting.exe
away from the _internal folder.

Where your data is stored
-------------------------

By default, your accounting data is stored here:

  %LOCALAPPDATA%\OpenAccounting\data

You can choose another data folder from PowerShell:

  OpenAccounting.exe --data-dir D:\OpenAccountingData

Useful options
--------------

  OpenAccounting.exe --no-browser
  OpenAccounting.exe --port 8790
  OpenAccounting.exe --data-dir D:\OpenAccountingData

PDF output
----------

This portable build generates PDFs with the built-in ReportLab renderer.
(The optional Chromium-based renderer is available when running from source
with Playwright installed; it is not included in the portable build.)

License and source code
-----------------------

Open Accounting is free and open-source software under AGPL-3.0-or-later.
The complete source code for this exact build is available at the tag above.
Licenses for the bundled third-party components are in
THIRD-PARTY-NOTICES.txt next to this file.

Source code:

  https://github.com/ai-coding-lab-au/open-accounting

Website:

  https://aicodinglab.com.au/open-accounting/

Contact:

  hello@aicodinglab.com.au

Important note
--------------

This is an early portable Windows build. Back up your data folder regularly.
