# 📊 Automated CSV Analyst

A Streamlit-based data analysis tool that lets you upload any CSV file, clean data, explore interactive charts, ask AI-powered questions, and export polished reports — all in your browser, no coding required.

---

## ✨ Features

- **Smart Analysis** — auto-detects dataset type, column roles, outliers, skew, correlations, and time series
- **Data Cleaning** — trim whitespace, standardize nulls, remove duplicates, fill missing values, convert types, detect dates
- **Interactive Charts** — Plotly-powered bar, histogram, scatter, box, line, heatmap charts with PNG export
- **Ask AI** — chat interface powered by Gemini API (with local fallback if no key is set)
- **Export Reports** — structured markdown report or AI-generated Gemini Summary in three styles
- **Mobile Friendly** — responsive layout that works on phones and tablets

---

## 🚀 Live Demo

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://your-app-url.streamlit.app)

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| UI & App | Streamlit |
| Data | Pandas, NumPy |
| Charts | Plotly |
| AI | Google Gemini API (`gemini-2.5-flash`) |
| Export | Markdown, Kaleido (PNG) |
| Language | Python 3.10+ |

---

## 📦 Installation

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/automated-csv-analyst.git
cd automated-csv-analyst

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
streamlit run final2.py
```

The app opens automatically at `http://localhost:8501`

---

## 🔑 Gemini API Key (Optional)

The app works without an API key using built-in Local Analyst Mode. To enable full AI features:

1. Get a free key at [aistudio.google.com](https://aistudio.google.com)
2. Paste it in the sidebar under **AI Mode → Gemini API**

**For deployment**, add it as a Streamlit secret instead of pasting it in the UI:

```toml
# .streamlit/secrets.toml
GEMINI_API_KEY = "your-key-here"
```

---

## 📁 Project Structure

```
automated-csv-analyst/
├── final2.py          # Main Streamlit app
├── requirements.txt   # Python dependencies
└── README.md
```

---

## 🗺️ How to Use

| Step | Tab | What to do |
|---|---|---|
| 1 | Overview | Upload your CSV and click Analyze Dataset |
| 2 | Data Preview | Review column types, nulls, and drill into any column |
| 3 | Clean Data | Apply recommended cleanup or use manual tools |
| 4 | Explore Charts | Browse auto-suggested charts or build your own |
| 5 | Ask AI | Chat with the AI about your data |
| 6 | Export Report | Download a markdown report in your preferred style |

---

## ☁️ Deploy to Streamlit Cloud

1. Fork this repo
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub and select this repo
4. Set **Main file path** to `final2.py`
5. Add your `GEMINI_API_KEY` under **Settings → Secrets**
6. Click Deploy

---

## 📄 License

MIT — free to use, modify, and distribute.

---

Built by **Tarun P**
