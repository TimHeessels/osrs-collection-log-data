# osrs-collection-log-data

Generates and publishes the wiki-sourced datasets used by the
[CollectionLogPopupEnhanced](https://github.com/TimHeessels/collection-log-popup-enhanced) RuneLite
plugin: per-item collection log completion percentages (`collection-log.json`) and per-source drop
rates (`drop-rates.json`).

A scheduled GitHub Actions workflow ([update-plugin-data.yml](.github/workflows/update-plugin-data.yml))
runs `generate-collection-log.py` and `generate-drop-rates.py` weekly and publishes both output files
to the orphan `data` branch:

- `https://raw.githubusercontent.com/TimHeessels/osrs-collection-log-data/data/collection-log.json`
- `https://raw.githubusercontent.com/TimHeessels/osrs-collection-log-data/data/drop-rates.json`

The plugin's `RemoteRarityOverridesUpdater` and `RemoteDropRateUpdater` fetch these at runtime.

To regenerate locally:

```
python generate-collection-log.py
python generate-drop-rates.py
```
