1. Decide on your target schema

Before touching any files, define what each final CSV must look like, regardless of metal or source format.

For example, for prices:

    For annual series: year,metal,price_usd_per_unit,unit,source_file

    For monthly series: year,month,date,metal,price_usd_per_unit,unit,source_file

Then, internally, build one master table per frequency:

    annual_prices_master.csv

    monthly_prices_master.csv

Your 66 final CSVs will just be filtered views (metal X, freq Y) of those master tables.
2. Build a file inventory (URLs + local paths)

USGS organizes commodity pages consistently, so first build an inventory of links rather than manually saving 66 files.

Recommended approach:

    Script to crawl the “Commodity statistics and information” page and follow each metal link.​

    On each metal’s page, parse all links that look like:

        *.xlsx, *.xls, *.pdf

        Often named like “Historical statistics”, “price”, “MCS YYYY”, “Minerals Yearbook” etc.

    For each file, classify:

        metal

        frequency (annual vs monthly, based on title / filename pattern)

        format (excel/pdf)

        url

Then download them all once into a local directory tree like:

    raw/usgs/<metal>/annual/*.xlsx|*.pdf

    raw/usgs/<metal>/monthly/*.xlsx|*.pdf

This “inventory step” is crucial because it lets you handle special cases and rerun the pipeline without re‑figuring out where things live.

If you don’t want to write a crawler, you can do a one‑time manual list in a CSV (metal, frequency, url) and let the script download from that.
3. Normalize Excel sources with a single parser

For Excel (ideal because they’re structured), use Python + pandas:

    For each Excel file:

        Open with pandas.read_excel (sometimes header row is not 0; you may need to detect the header row by scanning for a “Year” or “Month” column in the first few rows).

        Identify the price column(s):

            Column header often includes “Price” or “Unit value”, possibly with a unit like “dollars per metric ton” or “cents per pound”.

        Melt/reshape so you have tidy rows: one row per (date, series).

    Extract frequency:

        If there’s a year column only → annual.

        If there are year and month columns, or a date column → monthly.

    Standardize columns:

        Convert prices to floats and record units as a text column (do not silently convert currencies/units unless you have a consistent rule).

        Add metadata columns: metal, source_file.

    Append to the appropriate master table in memory (or on disk):

        annual_prices_master

        monthly_prices_master

Then at the end, write the masters once.
4. Handle PDF sources with templates, not ad‑hoc

PDFs are the annoying part. USGS PDFs tend to be semi‑regular (e.g., Mineral Commodity Summaries or Minerals Yearbook tables).

For PDFs, pick a PDF table tool that you can drive from code:

    Good options: camelot (for vector PDFs), tabula-py, pdfplumber in Python.

Workflow:

    For each metal PDF, manually inspect 1–2 representative files to understand:

        Which page the price table is on.

        Column headers and exact wording.

        How many header rows, footnotes, blanks.

    Create layout templates for recurring document types, e.g.:

        Template: MCS_price_table_type_A

            Use camelot.read_pdf(..., pages="3", flavor="lattice")

            Drop first 3 rows, keep columns “Year”, “Price ...” etc.

        Template: Yearbook_price_table_type_B

            Different page/area settings.

    In your inventory, add a column pdf_template (you can set it manually for weird ones; default by pattern matching title “Mineral Commodity Summaries YYYY – METAL”).

    For each PDF:

        Run the template’s extraction rule, get a DataFrame.

        Map columns to your standard schema.

        Append to the appropriate master table.

PDF is where most debugging will happen; you want something that fails loudly with “couldn’t locate price table” instead of silently giving wrong data.
5. Build and use a mapping layer

Because column names and units will differ across metals and across years, it’s worth maintaining a small metadata config (YAML/JSON) keyed by metal:

Example config/metals.json:

json
{
  "Aluminum": {
    "excel_price_columns": ["Price, ingot, average U.S. market (spot), cents per pound"],
    "pdf_table_templates": ["MCS_price_table_type_A"],
    "unit": "cents per pound"
  },
  "Copper": {
    "excel_price_columns": ["Unit value", "Price, refined, U.S. producer"],
    "pdf_table_templates": ["MCS_price_table_type_B"],
    "unit": "cents per pound"
  }
}

Your ETL code then uses this config to:

    Select the right column(s).

    Convert units if you choose a common one later (e.g., always dollars per metric ton).

    Apply the correct PDF parsing template.

This avoids scattering if/else logic throughout the code.
6. Generate the 66 final CSVs as a last step

Once you have annual_prices_master.csv and monthly_prices_master.csv:

    Load each master into pandas.

    Get your list of 33 metals.

    For each metal:

        Filter annual → write prices_annual_<metal>.csv.

        Filter monthly → write prices_monthly_<metal>.csv.

This step is trivial; all the complexity is upstream.
7. Practical stack and project layout

You’re comfortable with Linux, so I’d do:

    Language: Python.

    Libs:

        requests or httpx for downloads.

        pandas, openpyxl/xlrd for Excel.

        camelot or tabula-py (with Java) or pdfplumber for PDFs.

        beautifulsoup4 or lxml for HTML scraping.

Project layout:

    scripts/

        01_build_inventory.py (crawl pages, create inventory.csv)

        02_download_files.py (download everything in inventory.csv)

        03_extract_excel.py (append to master tables)

        04_extract_pdf.py (append to master tables)

        05_fanout_per_metal.py (generate 66 CSVs)

    config/metals.json

    data/raw/..., data/intermediate/..., data/final/...

Splitting into small scripts makes it easy to re‑run individual steps when (inevitably) a couple of PDFs don’t parse cleanly.
8. How to start with minimal upfront work

Given you have 33 metals, I’d bootstrap like this:

    Pick 2 metals (e.g., aluminum, copper) and implement the full pipeline for just those:

        Figure out Excel patterns.

        Build 1–2 PDF templates.

        Confirm that the final per‑metal CSVs look right.

    Generalize the extraction code so that adding a new metal is just:

        Add metadata to metals.json.

        Possibly add a new PDF template function if needed.

    Then add the remaining 31 metals using the same framework.

