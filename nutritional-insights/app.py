python
from flask import Flask, request, jsonify, render_template, Response
import os
from dotenv import load_dotenv
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import io
import hashlib
import json
import redis

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load and clean CSV using absolute path
csv_path = os.path.join(BASE_DIR, "nutrition.csv")
try:
    df = pd.read_csv(csv_path)
    print(f"✅ CSV loaded successfully from {csv_path}")
    
    df.dropna(inplace=True)
    df.drop_duplicates(inplace=True)
    
    DIET_TYPES = sorted(df["diet_type"].unique().tolist())
    
    diet_avg = df.groupby("diet_type")[["protein", "carbs", "fat", "fiber", "calories"]].mean().round(2)
    diet_counts = df.groupby("diet_type").size().to_dict()
    
    print(f"✅ Data processed: {len(df)} recipes, {len(DIET_TYPES)} diet types")
    
except Exception as e:
    print(f"❌ ERROR loading CSV: {e}")
    # Create empty dataframes as fallback to prevent app from crashing
    df = pd.DataFrame()
    DIET_TYPES = []
    diet_avg = pd.DataFrame()
    diet_counts = {}

DIET_COLORS = {
    "Vegan":         "#16a34a",
    "Keto":          "#2563eb",
    "Mediterranean": "#d97706",
    "Paleo":         "#dc2626",
    "Vegetarian":    "#7c3aed",
}


# Redis connection
_redis_client = None


def _to_bool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_redis():
    global _redis_client

    if _redis_client is None:
        redis_config = {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", "6379")),
            "password": os.getenv("REDIS_PASSWORD") or None,
            "ssl": _to_bool(os.getenv("REDIS_SSL"), default=False),
            "db": int(os.getenv("REDIS_DB", "0")),
            "decode_responses": True,
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
            "retry_on_timeout": True,
        }

        redis_username = os.getenv("REDIS_USERNAME")
        if redis_username:
            redis_config["username"] = redis_username

        _redis_client = redis.Redis(**redis_config)

    return _redis_client



def cache_get(key):
    try:
        return get_redis().get(key)
    except Exception:
        return None


def cache_set(key, value, ttl=3600):
    try:
        get_redis().setex(key, ttl, value)
    except Exception:
        pass

# Convert a matplotlib figure to a PNG image response
def fig_to_png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return Response(buf.read(), mimetype="image/png")


# Home page
@app.route("/")
def index():
    return render_template("index.html")


# Bar chart: average macronutrients per diet type
@app.route("/chart/bar")
def chart_bar():
    diets = diet_avg.index.tolist()
    x = np.arange(len(diets))
    width = 0.25

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(x - width, diet_avg["protein"], width, label="Protein (g)", color="#2563eb")
    ax.bar(x,         diet_avg["carbs"],   width, label="Carbs (g)",   color="#16a34a")
    ax.bar(x + width, diet_avg["fat"],     width, label="Fat (g)",     color="#f59e0b")

    ax.set_xticks(x)
    ax.set_xticklabels(diets, rotation=15, ha="right")
    ax.set_ylabel("Grams")
    ax.set_title("Average Macronutrients by Diet Type")
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    return fig_to_png(fig)


# Scatter plot: protein vs carbs for each recipe
@app.route("/chart/scatter")
def chart_scatter():
    fig, ax = plt.subplots(figsize=(7, 4))

    for diet in DIET_TYPES:
        subset = df[df["diet_type"] == diet]
        ax.scatter(
            subset["protein"],
            subset["carbs"],
            label=diet,
            color=DIET_COLORS[diet],
            alpha=0.7,
            s=40
        )

    ax.set_xlabel("Protein (g)")
    ax.set_ylabel("Carbs (g)")
    ax.set_title("Protein vs Carbs by Diet Type")
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()

    return fig_to_png(fig)


# Heatmap: nutrient levels across diet types
@app.route("/chart/heatmap")
def chart_heatmap():
    nutrients = ["protein", "carbs", "fat", "fiber", "calories"]
    data = diet_avg[nutrients]

    # Normalise each column 0-1 so colours are comparable
    normed = (data - data.min()) / (data.max() - data.min())

    fig, ax = plt.subplots(figsize=(7, 4))
    im = ax.imshow(normed.values, cmap=plt.cm.RdYlGn, aspect="auto", vmin=0, vmax=1)

    ax.set_xticks(range(len(nutrients)))
    ax.set_xticklabels([n.capitalize() for n in nutrients])
    ax.set_yticks(range(len(data.index)))
    ax.set_yticklabels(data.index.tolist())

    # Show the actual value inside each cell
    for i in range(len(data.index)):
        for j in range(len(nutrients)):
            ax.text(
                j, i,
                f"{data.values[i, j]:.1f}",
                ha="center",
                va="center",
                fontsize=8,
                color="black"
            )

    plt.colorbar(im, ax=ax, label="Relative intensity")
    ax.set_title("Nutrient Heatmap by Diet Type")
    fig.tight_layout()

    return fig_to_png(fig)


# Pie chart: how many recipes per diet type
@app.route("/chart/pie")
def chart_pie():
    labels = list(diet_counts.keys())
    sizes = list(diet_counts.values())
    colors = [DIET_COLORS[l] for l in labels]

    fig, ax = plt.subplots(figsize=(6, 4))
    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        colors=colors,
        autopct="%1.1f%%",
        startangle=140,
        wedgeprops={
            "edgecolor": "white",
            "linewidth": 2
        }
    )

    for t in autotexts:
        t.set_fontsize(8)

    ax.set_title("Recipe Distribution by Diet Type")
    fig.tight_layout()

    return fig_to_png(fig)


# API: average nutrition data, optionally filtered by diet
@app.route("/api/nutritional-insights")
def nutritional_insights():
    diet_filter = request.args.get("diet", "all").lower()
    cache_key = "insights:" + hashlib.md5(diet_filter.encode()).hexdigest()

    cached = cache_get(cache_key)
    if cached:
        result = json.loads(cached)
        result["source"] = "cache"
        return jsonify(result)

    if diet_filter != "all" and diet_filter.title() in DIET_TYPES:
        subset = diet_avg.loc[[diet_filter.title()]]
    else:
        subset = diet_avg

    result = {
        "status": "success",
        "filter": diet_filter,
        "source": "computed",
        "data": subset.to_dict(orient="index"),
    }

    cache_set(cache_key, json.dumps(result))
    return jsonify(result)


# API: paginated recipe list, optionally filtered by diet
@app.route("/api/recipes")
def recipes():
    diet_filter = request.args.get("diet", "all").lower()
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 10))
    cache_key = "recipes:" + hashlib.md5(f"{diet_filter}:{page}:{per_page}".encode()).hexdigest()

    cached = cache_get(cache_key)
    if cached:
        result = json.loads(cached)
        result["source"] = "cache"
        return jsonify(result)

    if diet_filter != "all" and diet_filter.title() in DIET_TYPES:
        subset = df[df["diet_type"] == diet_filter.title()]
    else:
        subset = df.copy()

    total = len(subset)
    paged = subset.iloc[(page - 1) * per_page : page * per_page]

    result = {
        "status": "success",
        "source": "computed",
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "data": paged.to_dict(orient="records"),
        "pie_data": {
            d: int(c) for d, c in diet_counts.items()
        }
    }

    cache_set(cache_key, json.dumps(result))
    return jsonify(result)


# API: diet clusters grouped by dominant macronutrient
@app.route("/api/clusters")
def clusters():
    cache_key = "clusters:all"

    cached = cache_get(cache_key)
    if cached:
        result = json.loads(cached)
        result["source"] = "cache"
        return jsonify(result)

    clusters_list = []

    for diet in DIET_TYPES:
        if diet not in diet_avg.index:
            continue

        row = diet_avg.loc[diet]
        dominant = max(["protein", "carbs", "fat"], key=lambda n: row[n])

        clusters_list.append(
            {
                "diet": diet,
                "dominant_macro": dominant,
                "avg_protein": round(float(row["protein"]), 2),
                "avg_carbs": round(float(row["carbs"]), 2),
                "avg_fat": round(float(row["fat"]), 2),
                "avg_calories": round(float(row["calories"]), 2),
                "recipe_count": diet_counts.get(diet, 0),
            }
        )

    result = {
        "status": "success",
        "source": "computed",
        "clusters": clusters_list
    }

    cache_set(cache_key, json.dumps(result))
    return jsonify(result)


if __name__ == "__main__":
    print("Starting Flask...")
    app.run(host="0.0.0.0", port=5000, debug=True)