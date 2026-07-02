# Multi-Driver Route Optimizer - Streamlit Version

This is the Streamlit version of the free route optimizer.

It accepts:

- Coordinates, for example `24.4539,54.3773`
- Google Maps links, including short `maps.app.goo.gl` links
- Normal addresses, using free OpenStreetMap/Nominatim geocoding

It uses:

- OSRM / OpenStreetMap for road travel times
- OR-Tools for multi-driver route optimization
- Streamlit for the web UI
- Google Maps links only for opening the finished driver route

No Google API key is required.

## Run locally

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

Open the local URL shown by Streamlit.

## Deploy free on Streamlit Community Cloud

1. Create a GitHub repository.
2. Upload these files to the repo:
   - `streamlit_app.py`
   - `requirements.txt`
   - `runtime.txt`
   - `.gitignore`
   - `README.md`
3. Go to Streamlit Community Cloud.
4. Create a new app from your GitHub repo.
5. Select `streamlit_app.py` as the main file.
6. Deploy.

## Password protect the app

The app supports a simple password gate.

On Streamlit Community Cloud:

1. Open the deployed app settings.
2. Go to **Secrets**.
3. Add either this:

```toml
APP_PASSWORD = "your-password-here"
```

or this hashed version:

```toml
APP_PASSWORD_HASH = "your-sha256-hash-here"
```

Generate the hash with:

```powershell
python -c "import hashlib; print(hashlib.sha256('your-password-here'.encode()).hexdigest())"
```

Do not commit real passwords or `secrets.toml` to GitHub.

## Optional routing settings

You can also add these to Streamlit Secrets:

```toml
ROUTING_PROVIDER = "osrm"
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
APP_USER_AGENT = "RouteOptimizerStreamlit/1.0 (small personal routing app; contact: you@example.com)"
GEOCODE_DELAY_SECONDS = "1.05"
MAX_OSRM_LOCATIONS = "80"
```

## Important limitations

- The public OSRM server is fine for testing and light usage, but not serious production usage.
- OSRM does not include live traffic.
- Nominatim geocoding is rate-limited; coordinates are faster and more reliable.
- For heavy business usage, self-host OSRM/Nominatim or use a paid routing provider.
