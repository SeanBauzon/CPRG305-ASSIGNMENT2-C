from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import io
import hashlib
import json
import os
import secrets
import smtplib
import ssl
import time
import redis
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from email.message import EmailMessage

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "nutritional-insights-secret-key")

oauth = OAuth(app)

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000")

app.config["GOOGLE_CLIENT_ID"] = os.getenv("GOOGLE_CLIENT_ID")
app.config["GOOGLE_CLIENT_SECRET"] = os.getenv("GOOGLE_CLIENT_SECRET")
app.config["GITHUB_CLIENT_ID"] = os.getenv("GITHUB_CLIENT_ID")
app.config["GITHUB_CLIENT_SECRET"] = os.getenv("GITHUB_CLIENT_SECRET")
# Load and clean CSV
df = pd.read_csv("nutrition.csv")
df.dropna(inplace=True)
df.drop_duplicates(inplace=True)

DIET_TYPES = sorted(df["diet_type"].unique().tolist())

diet_avg = df.groupby("diet_type")[["protein", "carbs", "fat", "fiber", "calories"]].mean().round(2)
diet_counts = df.groupby("diet_type").size().to_dict()

DIET_COLORS = {
    "Vegan":         "#16a34a",
    "Keto":          "#2563eb",
    "Mediterranean": "#d97706",
    "Paleo":         "#dc2626",
    "Vegetarian":    "#7c3aed",
}

google = oauth.register(
    name="google",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

github = oauth.register(
    name="github",
    client_id=app.config["GITHUB_CLIENT_ID"],
    client_secret=app.config["GITHUB_CLIENT_SECRET"],
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email"},
)

# In-memory metrics tracking
metrics = {
    "total_requests":   0,
    "cache_hits":       0,
    "cache_misses":     0,
    "api_calls":        {},
    "start_time":       time.time(),
}


# Redis connection
def get_redis():
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "YOUR_REDIS_HOST.redis.azure.net"),
        port=int(os.environ.get("REDIS_PORT", "10000")),
        username=os.environ.get("REDIS_USERNAME", "default"),
        password=os.environ.get("REDIS_PASSWORD", "YOUR_REDIS_PASSWORD"),
        ssl=os.environ.get("REDIS_SSL", "true").lower() == "true",
        decode_responses=True
    )


def cache_get(key):
    try:
        val = get_redis().get(key)
        if val:
            metrics["cache_hits"] += 1
        else:
            metrics["cache_misses"] += 1
        return val
    except:
        metrics["cache_misses"] += 1
        return None


def cache_set(key, value, ttl=3600):
    try:
        get_redis().setex(key, ttl, value)
    except:
        pass


def delete_cache_keys(patterns):
    deleted = 0
    redis_client = get_redis()
    for pattern in patterns:
        for key in redis_client.scan_iter(match=pattern):
            deleted += redis_client.delete(key)
    return deleted


def track(endpoint):
    metrics["total_requests"] += 1
    metrics["api_calls"][endpoint] = metrics["api_calls"].get(endpoint, 0) + 1


# Convert a matplotlib figure to a PNG image response
def fig_to_png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return Response(buf.read(), mimetype="image/png")


def send_two_factor_email(recipient_email, code):
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    mail_from = os.getenv("MAIL_FROM", smtp_username or "")
    smtp_use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

    if not smtp_host or not smtp_username or not smtp_password or not mail_from:
        raise RuntimeError("Email delivery is not configured. Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, and MAIL_FROM.")

    message = EmailMessage()
    message["Subject"] = "Your Nutritional Insights 2FA Code"
    message["From"] = mail_from
    message["To"] = recipient_email
    message.set_content(
        "Use this verification code to finish signing in to Nutritional Insights:\n\n"
        f"{code}\n\n"
        "This code expires soon. If you did not request it, you can ignore this email."
    )

    if smtp_use_tls:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_username, smtp_password)
            server.send_message(message)
    else:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context()) as server:
            server.login(smtp_username, smtp_password)
            server.send_message(message)


# Home page
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auth/google")
def login_google():
    if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
        return jsonify({"status": "error", "message": "Google OAuth is not configured."}), 500

    redirect_uri = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/github")
def login_github():
    if not app.config["GITHUB_CLIENT_ID"] or not app.config["GITHUB_CLIENT_SECRET"]:
        return jsonify({"status": "error", "message": "GitHub OAuth is not configured."}), 500

    redirect_uri = url_for("github_callback", _external=True)
    return github.authorize_redirect(redirect_uri)

# Bar chart
@app.route("/chart/bar")
def chart_bar():
    diet_filter = request.args.get("diet", "").strip()
    data = diet_avg.loc[[diet_filter]] if (diet_filter and diet_filter in DIET_TYPES) else diet_avg

    diets = data.index.tolist()
    x = np.arange(len(diets))
    width = 0.25

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(x - width, data["protein"], width, label="Protein (g)", color="#2563eb")
    ax.bar(x,         data["carbs"],   width, label="Carbs (g)",   color="#16a34a")
    ax.bar(x + width, data["fat"],     width, label="Fat (g)",     color="#f59e0b")

    ax.set_xticks(x)
    ax.set_xticklabels(diets, rotation=15, ha="right")
    ax.set_ylabel("Grams")
    ax.set_title("Average Macronutrients by Diet Type")
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig_to_png(fig)


# Scatter plot
@app.route("/chart/scatter")
def chart_scatter():
    diet_filter = request.args.get("diet", "").strip()
    subset_df = df[df["diet_type"] == diet_filter] if (diet_filter and diet_filter in DIET_TYPES) else df
    diets_to_show = [diet_filter] if (diet_filter and diet_filter in DIET_TYPES) else DIET_TYPES

    fig, ax = plt.subplots(figsize=(7, 4))
    for diet in diets_to_show:
        subset = subset_df[subset_df["diet_type"] == diet]
        ax.scatter(subset["protein"], subset["carbs"],
                   label=diet, color=DIET_COLORS[diet], alpha=0.7, s=40)

    ax.set_xlabel("Protein (g)")
    ax.set_ylabel("Carbs (g)")
    ax.set_title("Protein vs Carbs by Diet Type")
    ax.legend(fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    return fig_to_png(fig)


# Heatmap
@app.route("/chart/heatmap")
def chart_heatmap():
    diet_filter = request.args.get("diet", "").strip()
    if diet_filter and diet_filter in DIET_TYPES:
        data = diet_avg.loc[[diet_filter], ["protein", "carbs", "fat", "fiber", "calories"]]
    else:
        data = diet_avg[["protein", "carbs", "fat", "fiber", "calories"]]

    nutrients = data.columns.tolist()
    normed = (data - data.min()) / (data.max() - data.min()).replace(0, 1)

    fig, ax = plt.subplots(figsize=(7, 4))
    im = ax.imshow(normed.values, cmap=plt.cm.RdYlGn, aspect="auto", vmin=0, vmax=1)

    ax.set_xticks(range(len(nutrients)))
    ax.set_xticklabels([n.capitalize() for n in nutrients])
    ax.set_yticks(range(len(data.index)))
    ax.set_yticklabels(data.index.tolist())

    for i in range(len(data.index)):
        for j in range(len(nutrients)):
            ax.text(j, i, f"{data.values[i, j]:.1f}",
                    ha="center", va="center", fontsize=8, color="black")

    plt.colorbar(im, ax=ax, label="Relative intensity")
    ax.set_title("Nutrient Heatmap by Diet Type")
    fig.tight_layout()
    return fig_to_png(fig)


# Pie chart
@app.route("/chart/pie")
def chart_pie():
    diet_filter = request.args.get("diet", "").strip()
    counts = {diet_filter: diet_counts.get(diet_filter, 0)} if (diet_filter and diet_filter in DIET_TYPES) else diet_counts

    labels = list(counts.keys())
    sizes  = list(counts.values())
    colors = [DIET_COLORS[l] for l in labels]

    fig, ax = plt.subplots(figsize=(6, 4))
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=colors,
        autopct="%1.1f%%", startangle=140,
        wedgeprops={"edgecolor": "white", "linewidth": 2}
    )
    for t in autotexts:
        t.set_fontsize(8)

    ax.set_title("Recipe Distribution by Diet Type")
    fig.tight_layout()
    return fig_to_png(fig)


# API: nutritional insights
@app.route("/api/nutritional-insights")
def nutritional_insights():
    track("nutritional-insights")
    diet_filter = request.args.get("diet", "all").lower()
    cache_key = "insights:" + hashlib.md5(diet_filter.encode()).hexdigest()

    cached = cache_get(cache_key)
    if cached:
        result = json.loads(cached)
        result["source"] = "cache"
        return jsonify(result)

    subset = diet_avg.loc[[diet_filter.title()]] if (diet_filter != "all" and diet_filter.title() in DIET_TYPES) else diet_avg

    result = {
        "status": "success",
        "filter": diet_filter,
        "source": "computed",
        "data": subset.to_dict(orient="index"),
    }
    cache_set(cache_key, json.dumps(result))
    return jsonify(result)


# API: recipes with pagination
@app.route("/api/recipes")
def recipes():
    track("recipes")
    diet_filter = request.args.get("diet", "all").lower()
    page        = int(request.args.get("page", 1))
    per_page    = int(request.args.get("per_page", 10))
    cache_key   = "recipes:" + hashlib.md5(f"{diet_filter}:{page}".encode()).hexdigest()

    cached = cache_get(cache_key)
    if cached:
        result = json.loads(cached)
        result["source"] = "cache"
        return jsonify(result)

    subset = df[df["diet_type"] == diet_filter.title()] if (diet_filter != "all" and diet_filter.title() in DIET_TYPES) else df.copy()
    total  = len(subset)
    paged  = subset.iloc[(page - 1) * per_page : page * per_page]

    result = {
        "status": "success",
        "source": "computed",
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "data": paged.to_dict(orient="records"),
        "pie_data": {d: int(c) for d, c in diet_counts.items()}
    }
    cache_set(cache_key, json.dumps(result))
    return jsonify(result)


# API: clusters
@app.route("/api/clusters")
def clusters():
    track("clusters")
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
        row      = diet_avg.loc[diet]
        dominant = max(["protein", "carbs", "fat"], key=lambda n: row[n])
        clusters_list.append(
            {
                "diet":           diet,
                "dominant_macro": dominant,
                "avg_protein":    round(float(row["protein"]),  2),
                "avg_carbs":      round(float(row["carbs"]),    2),
                "avg_fat":        round(float(row["fat"]),      2),
                "avg_calories":   round(float(row["calories"]), 2),
                "recipe_count":   diet_counts.get(diet, 0),
            }
        )

    result = {"status": "success", "source": "computed", "clusters": clusters_list}
    cache_set(cache_key, json.dumps(result))
    return jsonify(result)


# API: security status
@app.route("/api/security-status")
def security_status():
    return jsonify(
        {
            "status":         "success",
            "encryption":     "Enabled",
            "access_control": "Secure",
            "compliance":     "GDPR Compliant",
            "ssl":            True,
            "redis_encrypted": True,
        }
    )


# API: live metrics & monitoring
@app.route("/api/metrics")
def get_metrics():
    uptime_seconds = int(time.time() - metrics["start_time"])
    hours   = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60
    seconds = uptime_seconds % 60

    total = metrics["cache_hits"] + metrics["cache_misses"]
    hit_rate = round((metrics["cache_hits"] / total * 100), 1) if total > 0 else 0.0

    return jsonify(
        {
            "status":          "success",
            "uptime":          f"{hours:02d}:{minutes:02d}:{seconds:02d}",
            "total_requests":  metrics["total_requests"],
            "cache_hits":      metrics["cache_hits"],
            "cache_misses":    metrics["cache_misses"],
            "cache_hit_rate":  f"{hit_rate}%",
            "total_recipes":   len(df),
            "total_diets":     len(DIET_TYPES),
            "api_calls":       metrics["api_calls"],
        }
    )


# API: CI/CD deployment status
@app.route("/api/deployment-status")
def deployment_status():
    return jsonify(
        {
            "status":       "success",
            "pipeline":     "GitHub Actions",
            "last_deploy":  "Triggered on push to main",
            "build_status": "Passing",
            "deploy_target": "Azure App Service",
            "redis_cache":  "Azure Redis Cache (Basic C0)",
            "python":       "3.11",
            "auto_deploy":  True,
        }
    )


# # API: simulated OAuth login
# @app.route("/api/oauth-login", methods=["POST"])
# def oauth_login():
#     data     = request.get_json()
#     provider = data.get("provider", "unknown")

#     session["user"] = {
#         "provider":  provider,
#         "email":     f"demo@{provider.lower()}.com",
#         "logged_in": True
#     }

#     return jsonify(
#         {
#             "status":  "success",
#             "message": f"Simulated login with {provider}",
#             "user":    session["user"]
#         }
#     )

@app.route("/auth/google/callback")
def google_callback():
    token = google.authorize_access_token()
    if not token:
        return jsonify({"status": "error", "message": "Google authorization failed."}), 400

    user_info = token.get("userinfo")
    if not user_info:
        user_info = google.get("userinfo", token=token).json()

    session["user"] = {
        "email": user_info.get("email"),
        "name": user_info.get("name") or user_info.get("given_name"),
        "provider": "google",
        "2fa_verified": False,
    }
    session.pop("2fa_secret", None)

    return redirect(url_for("index", login="google", twofa="required"))


@app.route("/auth/github/callback")
def github_callback():
    token = github.authorize_access_token()
    if not token:
        return jsonify({"status": "error", "message": "GitHub authorization failed."}), 400

    user = github.get("user", token=token).json()

    # fallback for email (GitHub sometimes hides it)
    email = user.get("email")
    if not email:
        emails = github.get("user/emails", token=token).json()
        email = next((e["email"] for e in emails if e["primary"]), None)

    session["user"] = {
        "email": email,
        "name": user.get("login"),
        "provider": "github",
        "2fa_verified": True
    }
    session.pop("2fa_secret", None)

    return redirect(url_for("index", login="github"))

# # API: simulated 2FA verification
# @app.route("/api/verify-2fa", methods=["POST"])
# def verify_2fa():
#     data = request.get_json()
#     code = data.get("code", "")

#     if len(code) == 6 and code.isdigit():
#         return jsonify(
#             {
#                 "status":   "success",
#                 "message":  "2FA verification successful",
#                 "verified": True
#             }
#         )

#     return jsonify(
#         {
#             "status":   "error",
#             "message":  "Invalid code. Enter a 6-digit number.",
#             "verified": False
#         }
#     ), 400

@app.route("/api/send-2fa", methods=["POST"])
def send_2fa():
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 401

    if session["user"].get("provider") != "google":
        return jsonify({"status": "error", "message": "2FA is only required for Google login"}), 400

    recipient_email = session["user"].get("email")
    if not recipient_email:
        return jsonify({"status": "error", "message": "No email address is available for this account"}), 400

    code = f"{secrets.randbelow(1000000):06d}"
    session["2fa_code"] = code
    session["2fa_expires_at"] = int(time.time()) + 600

    try:
        send_two_factor_email(recipient_email, code)
    except Exception as exc:
        session.pop("2fa_code", None)
        session.pop("2fa_expires_at", None)
        return jsonify({"status": "error", "message": f"Failed to send 2FA email: {exc}"}), 500

    return jsonify({"status": "success", "message": f"2FA code sent to {recipient_email}"})

@app.route("/api/verify-2fa", methods=["POST"])
def verify_2fa():
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()

    saved_code = session.get("2fa_code")
    expires_at = session.get("2fa_expires_at")
    if not saved_code or not expires_at:
        return jsonify({"status": "error", "message": "No 2FA session", "verified": False}), 400

    if session.get("user", {}).get("provider") != "google":
        return jsonify({"status": "error", "message": "2FA is only available for Google login", "verified": False}), 400

    if int(time.time()) > int(expires_at):
        session.pop("2fa_code", None)
        session.pop("2fa_expires_at", None)
        return jsonify({"status": "error", "message": "2FA code expired. Please request a new code.", "verified": False}), 400

    if code == saved_code:
        user = session.get("user", {})
        user["2fa_verified"] = True
        session["user"] = user
        session.pop("2fa_code", None)
        session.pop("2fa_expires_at", None)
        session.modified = True
        return jsonify({"status": "success", "message": "2FA success", "verified": True})

    return jsonify({"status": "error", "message": "Invalid code", "verified": False}), 400


# API: cloud resource cleanup
@app.route("/api/cleanup", methods=["POST"])
def cleanup():
    cleaned = []
    warnings = []

    try:
        deleted_keys = delete_cache_keys(["insights:*", "recipes:*", "clusters:*"])
        cleaned.append(f"Removed {deleted_keys} Redis cache key(s)")
    except Exception as exc:
        warnings.append(f"Redis cleanup skipped: {exc}")

    metrics["cache_hits"] = 0
    metrics["cache_misses"] = 0
    metrics["api_calls"] = {}
    cleaned.append("Reset in-memory monitoring counters")

    status = "success" if not warnings else "partial"
    message = "Cloud resource cleanup completed"
    if warnings:
        message = "Cloud cleanup completed with warnings"

    return jsonify(
        {
            "status": status,
            "message": message,
            "actions": cleaned,
            "warnings": warnings,
        }
    )


if __name__ == "__main__":
    print("Starting Flask...")
    app.run(host="0.0.0.0", port=5000, debug=True)