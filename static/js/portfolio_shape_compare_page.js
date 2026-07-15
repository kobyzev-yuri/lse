(function () {
        var COLORS = ["#38bdf8", "#4ade80", "#fbbf24", "#f472b6", "#a78bfa", "#fb923c", "#22d3ee", "#e879f9", "#86efac", "#f87171", "#c084fc", "#2dd4bf"];
        var sparkCloses = {};
        var stEl = document.getElementById("statusLine");
        var metaEl = document.getElementById("metaLine");
        function st(m) { try { if (stEl) stEl.textContent = String(m); } catch (e0) {} }
        function colorFor(i) { return COLORS[i % COLORS.length]; }
        st("js-0 ready - pick 2-3");
        if (metaEl) metaEl.textContent = "no SSR embed; sparks on demand";

        function fetchJson(url, ms) {
            ms = ms || 12000;
            var ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
            var timedOut = false;
            var timer = setTimeout(function () {
                timedOut = true;
                try { if (ctrl) ctrl.abort(); } catch (e0) {}
            }, ms);
            var opts = { cache: "no-store" };
            if (ctrl) opts.signal = ctrl.signal;
            return fetch(url, opts).then(function (r) {
                return r.json().then(function (body) {
                    clearTimeout(timer);
                    return { ok: r.ok, status: r.status, body: body };
                }, function () {
                    clearTimeout(timer);
                    return { ok: r.ok, status: r.status, body: {} };
                });
            }).catch(function (e) {
                clearTimeout(timer);
                if (e && e.name === "AbortError") {
                    throw new Error(timedOut ? ("timeout " + ms + "ms") : "aborted");
                }
                throw new Error(e && e.message ? e.message : String(e));
            });
        }

        function sparkSvg(seriesList) {
            var w = 640, h = 180, pad = 8, n = 0;
            seriesList.forEach(function (s) {
                if ((s.values || []).length > n) n = s.values.length;
            });
            if (n < 2) return "<svg class='spark' viewBox='0 0 " + w + " " + h + "'></svg>";
            var ymin = Infinity, ymax = -Infinity;
            seriesList.forEach(function (s) {
                (s.values || []).forEach(function (v) {
                    if (v == null || !isFinite(v)) return;
                    if (v < ymin) ymin = v;
                    if (v > ymax) ymax = v;
                });
            });
            if (!isFinite(ymin) || !isFinite(ymax) || ymin === ymax) { ymin = 0; ymax = 1; }
            var paths = seriesList.map(function (s, idx) {
                var stroke = s.color || colorFor(idx);
                var pts = [];
                (s.values || []).forEach(function (v, i) {
                    if (v == null || !isFinite(v)) return;
                    var x = pad + (i / (n - 1)) * (w - 2 * pad);
                    var y = h - pad - ((v - ymin) / (ymax - ymin)) * (h - 2 * pad);
                    pts.push(x.toFixed(1) + "," + y.toFixed(1));
                });
                if (pts.length < 2) return "";
                return "<polyline fill='none' stroke='" + stroke + "' stroke-width='1.8' points='" + pts.join(" ") + "'/>";
            }).join("");
            return "<svg class='spark' viewBox='0 0 " + w + " " + h + "' preserveAspectRatio='none'>" + paths + "</svg>";
        }

        function legendHtml(seriesList) {
            return "<div class='ov-legend'>" + seriesList.map(function (s) {
                return "<span><i style='background:" + (s.color || colorFor(0)) + "'></i>" + (s.ticker || "?") + "</span>";
            }).join("") + "</div>";
        }

        function normCloses(closes) {
            var out = [], first = null;
            (closes || []).forEach(function (c0) {
                var c = Number(c0);
                if (!isFinite(c) || c <= 0) { out.push(null); return; }
                if (first == null) first = c;
                out.push(c / first);
            });
            return out;
        }

        function pearson(a, b) {
            var xs = [], ys = [];
            var n = Math.min((a || []).length, (b || []).length);
            for (var i = 0; i < n; i++) {
                var x = a[i], y = b[i];
                if (x == null || y == null || !isFinite(x) || !isFinite(y)) continue;
                xs.push(x); ys.push(y);
            }
            if (xs.length < 5) return null;
            var mx = 0, my = 0;
            for (var j = 0; j < xs.length; j++) { mx += xs[j]; my += ys[j]; }
            mx /= xs.length; my /= ys.length;
            var num = 0, dx = 0, dy = 0;
            for (var k = 0; k < xs.length; k++) {
                var vx = xs[k] - mx, vy = ys[k] - my;
                num += vx * vy; dx += vx * vx; dy += vy * vy;
            }
            if (!(dx > 0) || !(dy > 0)) return null;
            return num / Math.sqrt(dx * dy);
        }

        function pairLine(series) {
            var parts = [];
            for (var i = 0; i < series.length; i++) {
                for (var j = i + 1; j < series.length; j++) {
                    var c = pearson(series[i].values, series[j].values);
                    if (c == null) continue;
                    parts.push(series[i].ticker + "-" + series[j].ticker +
                        " (corr " + c.toFixed(3) + ", d " + (1 - c).toFixed(3) + ")");
                }
            }
            return parts.join(" | ");
        }

        function picked() {
            var picks = ["cmpA", "cmpB", "cmpC"].map(function (id) {
                var el = document.getElementById(id);
                return el ? String(el.value || "").toUpperCase() : "";
            }).filter(Boolean);
            var seen = {};
            return picks.filter(function (t) {
                if (seen[t]) return false;
                seen[t] = true;
                return true;
            }).slice(0, 3);
        }

        function paintFromCache() {
            var root = document.getElementById("compareRoot");
            if (!root) return;
            var picks = picked();
            if (picks.length < 2) {
                root.innerHTML = "<p class='muted'>Выберите минимум 2 тикера.</p>";
                return false;
            }
            var series = [], miss = [];
            picks.forEach(function (t, idx) {
                var closes = sparkCloses[t];
                if (!closes || !closes.length) { miss.push(t); return; }
                var vals = normCloses(closes);
                if (vals.length < 2) { miss.push(t); return; }
                series.push({ ticker: t, values: vals, color: colorFor(idx) });
            });
            if (series.length < 2) return false;
            var pairs = pairLine(series);
            root.innerHTML = "<div class='chart-section'><h2>Compare (norm) · " +
                series.map(function (s) { return s.ticker; }).join(" · ") + "</h2>" +
                sparkSvg(series) + legendHtml(series) +
                (pairs ? "<div class='pair'>" + pairs + "</div>" : "") +
                (miss.length ? "<p class='muted'>missing: " + miss.join(", ") + "</p>" : "") +
                "</div>";
            st("overlay " + series.map(function (s) { return s.ticker; }).join("/"));
            return true;
        }

        function paintOverlay() {
            var picks = picked();
            if (picks.length < 2) {
                document.getElementById("compareRoot").innerHTML = "<p class='muted'>Выберите минимум 2 тикера.</p>";
                return;
            }
            var need = picks.filter(function (t) { return !(sparkCloses[t] && sparkCloses[t].length); });
            if (!need.length && paintFromCache()) return;
            refreshSelected(true);
        }

        function refreshSelected(autoPaint) {
            var picks = picked();
            var lookEl = document.getElementById("lookbackSel");
            var lookback = lookEl ? lookEl.value : "126";
            if (picks.length < 1) { st("pick tickers first"); return; }
            var url = "/api/portfolio/shape-clusters/sparks?lookback_days=" + encodeURIComponent(lookback) +
                "&tickers=" + encodeURIComponent(picks.join(","));
            st("refresh " + picks.join(",") + "...");
            document.getElementById("compareRoot").innerHTML =
                "<p class='muted'>Загрузка " + picks.join(", ") + "...</p>";
            fetchJson(url, 12000).then(function (pack) {
                if (!pack.ok) throw new Error((pack.body && pack.body.detail) || ("HTTP " + pack.status));
                var sc = (pack.body && pack.body.spark_closes) || {};
                Object.keys(sc).forEach(function (t) { sparkCloses[t] = sc[t]; });
                if (metaEl) metaEl.textContent = "sparks=" + Object.keys(sparkCloses).length;
                st("refreshed " + Object.keys(sc).join(","));
                if (autoPaint) paintFromCache();
            }).catch(function (e) {
                st("refresh fail: " + (e && e.message ? e.message : String(e)) + " — нажмите ещё раз");
            });
        }

        var reloadBtn = document.getElementById("reloadBtn");
        var cmpBtn = document.getElementById("cmpBtn");
        var cmpClearBtn = document.getElementById("cmpClearBtn");
        if (reloadBtn) reloadBtn.addEventListener("click", function () { refreshSelected(true); });
        if (cmpBtn) cmpBtn.addEventListener("click", paintOverlay);
        if (cmpClearBtn) {
            cmpClearBtn.addEventListener("click", function () {
                ["cmpA", "cmpB", "cmpC"].forEach(function (id) {
                    var el = document.getElementById(id);
                    if (el) el.value = "";
                });
                document.getElementById("compareRoot").innerHTML = "";
                st("cleared");
            });
        }
    })();
