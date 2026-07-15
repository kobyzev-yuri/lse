/* shape-clusters UI: ASCII-only, no async/await */
(function () {
    var chartInstances = [];
    var currentClusterId = null;
    var lastReport = null;
    var mapHeartbeat = null;
    var chartJsPromise = null;
    var boardLoadSeq = 0;
    var simDebounce = null;
    var COLORS = ['#38bdf8', '#4ade80', '#fbbf24', '#f472b6', '#a78bfa', '#fb923c', '#22d3ee', '#e879f9', '#86efac', '#f87171', '#c084fc', '#2dd4bf'];

    function destroyCharts() {
        chartInstances.forEach(function (ch) { try { ch.destroy(); } catch (e) {} });
        chartInstances = [];
    }

    function clearChartsRoot() {
        var root = document.getElementById('chartsRoot');
        if (root && window.Chart && typeof Chart.getChart === 'function') {
            Array.prototype.forEach.call(root.querySelectorAll('canvas'), function (cv) {
                try {
                    var ch = Chart.getChart(cv);
                    if (ch) ch.destroy();
                } catch (e0) {}
            });
        }
        destroyCharts();
        if (root) root.innerHTML = '';
    }

    function mountCanvas(section) {
        var wrap = document.createElement('div');
        wrap.className = 'chart-wrap';
        var cv = document.createElement('canvas');
        wrap.appendChild(cv);
        section.appendChild(wrap);
        return cv;
    }

    function safeCreateChart(canvas, factory) {
        try {
            if (window.Chart && typeof Chart.getChart === 'function') {
                var prev = Chart.getChart(canvas);
                if (prev) {
                    try { prev.destroy(); } catch (e0) {}
                    chartInstances = chartInstances.filter(function (ch) { return ch !== prev; });
                }
            }
            var ch = factory();
            if (ch) chartInstances.push(ch);
            return ch;
        } catch (e) {
            return null;
        }
    }

    function colorFor(i) { return COLORS[i % COLORS.length]; }

    function setStatus(msg) {
        var el = document.getElementById('statusLine');
        if (el) el.textContent = msg;
    }

    function stopHeartbeat() {
        if (mapHeartbeat) { clearInterval(mapHeartbeat); mapHeartbeat = null; }
    }

    function startHeartbeat(base) {
        stopHeartbeat();
        var t0 = Date.now();
        setStatus(base + ' 0s');
        mapHeartbeat = setInterval(function () {
            setStatus(base + ' ' + Math.round((Date.now() - t0) / 1000) + 's');
        }, 500);
    }

    function loadScript(url, timeoutMs) {
        return new Promise(function (resolve, reject) {
            var s = document.createElement('script');
            s.src = url;
            s.async = true;
            var done = false;
            var timer = setTimeout(function () {
                if (done) return;
                done = true;
                try { s.remove(); } catch (e0) {}
                reject(new Error('timeout ' + url));
            }, timeoutMs || 12000);
            s.onload = function () {
                if (done) return;
                done = true;
                clearTimeout(timer);
                resolve();
            };
            s.onerror = function () {
                if (done) return;
                done = true;
                clearTimeout(timer);
                reject(new Error('load fail ' + url));
            };
            document.head.appendChild(s);
        });
    }

    function ensureChartJs() {
        if (window.Chart) return Promise.resolve();
        if (chartJsPromise) return chartJsPromise;
        var urls = [
            '/static/vendor/chart.umd.min.js',
            'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js',
            'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js'
        ];
        chartJsPromise = loadScript(urls[0], 12000).then(function () {
            if (!window.Chart) throw new Error('no Chart');
        }).catch(function () {
            return loadScript(urls[1], 12000).then(function () {
                if (!window.Chart) throw new Error('no Chart');
            });
        }).catch(function () {
            return loadScript(urls[2], 12000).then(function () {
                if (!window.Chart) throw new Error('no Chart');
            });
        }).catch(function (err) {
            chartJsPromise = null;
            throw err || new Error('Chart.js fail');
        });
        return chartJsPromise;
    }

    function fetchJsonTimeout(url, ms) {
        ms = ms || 12000;
        return new Promise(function (resolve, reject) {
            var settled = false;
            var timer = setTimeout(function () {
                if (settled) return;
                settled = true;
                reject(new Error('timeout ' + ms + 'ms'));
            }, ms);
            fetch(url, { cache: 'no-store' }).then(function (r) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(r);
            }).catch(function (e) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                reject(e);
            });
        });
    }

    function buildOverlayChart(canvas, overlay) {
        var labels = overlay.labels || [];
        var series = overlay.series || [];
        return safeCreateChart(canvas, function () {
            return new Chart(canvas, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: series.map(function (s, i) {
                        return {
                            label: s.ticker,
                            data: s.values || [],
                            borderColor: colorFor(i),
                            tension: 0.1,
                            spanGaps: true,
                            pointRadius: 0,
                            borderWidth: 1.6
                        };
                    })
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    plugins: {
                        title: { display: true, text: 'Norm price', color: '#94a3b8', font: { size: 11 } },
                        legend: { labels: { color: '#cbd5e1', boxWidth: 10, font: { size: 10 } } }
                    },
                    scales: {
                        x: { ticks: { color: '#94a3b8', maxTicksLimit: 10 } },
                        y: { ticks: { color: '#94a3b8' } }
                    }
                }
            });
        });
    }

    function buildPriceChart(canvas, bars) {
        var labels = bars.map(function (b) { return String(b.date).slice(0, 10); });
        var closes = bars.map(function (b) { return b.close != null ? Number(b.close) : null; });
        return safeCreateChart(canvas, function () {
            return new Chart(canvas, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{ label: 'Close', data: closes, borderColor: '#38bdf8', tension: 0.1, spanGaps: true, pointRadius: 0 }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: false,
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { ticks: { color: '#94a3b8', maxTicksLimit: 10 } },
                        y: { ticks: { color: '#94a3b8' } }
                    }
                }
            });
        });
    }

    function renderBoard(clusters) {
        var board = document.getElementById('clusterBoard');
        if (!board) return;
        board.innerHTML = '';
        (clusters || []).forEach(function (c, idx) {
            var card = document.createElement('div');
            card.className = 'cluster-card' + (Number(c.cluster_id) === Number(currentClusterId) ? ' active' : '');
            card.style.borderLeftColor = colorFor(idx);
            card.setAttribute('data-cluster-id', String(c.cluster_id));

            var title = document.createElement('div');
            title.className = 'title';
            title.textContent = c.label || ('C' + c.cluster_id);

            var meta = document.createElement('div');
            meta.className = 'meta';
            meta.textContent = (c.size || 0) + ' tickers - click for charts';

            var ticks = document.createElement('div');
            ticks.className = 'tickers';
            (c.tickers || []).forEach(function (t) {
                var s = document.createElement('span');
                if (t === c.medoid) s.className = 'medoid';
                s.textContent = t;
                ticks.appendChild(s);
            });

            card.appendChild(title);
            card.appendChild(meta);
            card.appendChild(ticks);

            var pairs = (c.strong_pairs || []).map(function (p) {
                return p.a + '-' + p.b + ' (' + p.corr + ')';
            }).join(' | ');
            if (pairs) {
                var pEl = document.createElement('div');
                pEl.className = 'pair';
                pEl.textContent = pairs;
                card.appendChild(pEl);
            }

            card.addEventListener('click', function () {
                selectCluster(Number(c.cluster_id));
            });
            board.appendChild(card);
        });
    }

    function markActiveCard() {
        Array.prototype.forEach.call(document.querySelectorAll('.cluster-card'), function (card) {
            var id = card.getAttribute('data-cluster-id');
            if (Number(id) === Number(currentClusterId)) card.classList.add('active');
            else card.classList.remove('active');
        });
    }

    function buildOverlayFromCharts(items, maxSeries) {
        var byTicker = {};
        var allDates = {};
        (items || []).forEach(function (it) {
            var bars = it.bars || [];
            if (!bars.length) return;
            var first = null;
            var map = {};
            bars.forEach(function (b) {
                var d = String(b.date || '').slice(0, 10);
                var c = Number(b.close);
                if (!d || !isFinite(c) || c <= 0) return;
                if (first == null) first = c;
                map[d] = c / first;
                allDates[d] = true;
            });
            if (first != null) byTicker[it.ticker] = map;
        });
        var labels = Object.keys(allDates).sort();
        var tickers = Object.keys(byTicker);
        if (tickers.length > (maxSeries || 12)) tickers = tickers.slice(0, maxSeries || 12);
        return {
            labels: labels,
            series: tickers.map(function (t) {
                return {
                    ticker: t,
                    values: labels.map(function (d) {
                        var v = byTicker[t][d];
                        return (v == null || !isFinite(v)) ? null : v;
                    })
                };
            })
        };
    }

    function fetchChartsBatch(tickers, days) {
        var url = '/api/portfolio/shape-clusters/charts?days=' + encodeURIComponent(days) +
            '&tickers=' + encodeURIComponent(tickers.join(','));
        function once() {
            return fetchJsonTimeout(url, 45000).then(function (cr) {
                if (!cr.ok) throw new Error('HTTP ' + cr.status);
                return cr.json();
            }).then(function (pack) {
                return pack.charts || [];
            });
        }
        return once().catch(function () { return once(); });
    }

    function stillThisCluster(clusterId) {
        return Number(currentClusterId) === Number(clusterId);
    }

    function appendDailyCharts(items, clusterId) {
        var root = document.getElementById('chartsRoot');
        var drawn = 0;
        (items || []).forEach(function (item) {
            if (!stillThisCluster(clusterId)) return;
            var sec = document.createElement('div');
            sec.className = 'chart-section';
            var h = document.createElement('h2');
            h.textContent = item.ticker || '-';
            sec.appendChild(h);
            root.appendChild(sec);
            if (!(item.bars || []).length) {
                var miss = document.createElement('p');
                miss.className = 'muted';
                miss.textContent = 'No quotes';
                sec.appendChild(miss);
                return;
            }
            var cv = mountCanvas(sec);
            if (buildPriceChart(cv, item.bars)) drawn += 1;
        });
        return drawn;
    }

    function paintOverlay(collected) {
        try {
            var root = document.getElementById('chartsRoot');
            var existing = document.getElementById('overlaySection');
            if (existing) {
                Array.prototype.forEach.call(existing.querySelectorAll('canvas'), function (cv) {
                    try {
                        if (window.Chart && Chart.getChart) {
                            var oldCh = Chart.getChart(cv);
                            if (oldCh) oldCh.destroy();
                        }
                    } catch (e0) {}
                });
                existing.remove();
            }
            var ov = buildOverlayFromCharts(collected, 8);
            if (ov.series.length < 2) return;
            var ovSec = document.createElement('div');
            ovSec.id = 'overlaySection';
            ovSec.className = 'chart-section overlay';
            var ovTitle = document.createElement('h2');
            ovTitle.textContent = 'Overlay';
            ovSec.appendChild(ovTitle);
            var ovCanvas = mountCanvas(ovSec);
            if (root.firstChild) root.insertBefore(ovSec, root.firstChild);
            else root.appendChild(ovSec);
            buildOverlayChart(ovCanvas, ov);
        } catch (e) {}
    }

    function selectCluster(clusterId) {
        clusterId = Number(clusterId);
        currentClusterId = clusterId;
        markActiveCard();

        var head = document.getElementById('detailHead');
        var daysEl = document.getElementById('daysSel');
        var days = daysEl ? daysEl.value : '180';
        var selected = null;
        ((lastReport && lastReport.clusters) || []).forEach(function (c) {
            if (Number(c.cluster_id) === clusterId) selected = c;
        });
        if (!selected) {
            setStatus('Cluster not found');
            return;
        }

        head.style.display = 'block';
        head.textContent = 'Charts: ' + (selected.label || '') + ' | ' + (selected.tickers || []).join(', ');
        clearChartsRoot();
        try { head.scrollIntoView(true); } catch (e1) {}
        setStatus('Loading Chart.js...');

        var members = selected.tickers || [];
        var withOverlay = members.length > 1;
        var maxDaily = 12;
        var showMembers = members.slice(0, maxDaily);
        var hiddenN = members.length - showMembers.length;

        ensureChartJs().then(function () {
            if (!stillThisCluster(clusterId)) return null;
            setStatus('Fetching ' + showMembers.length + ' series...');
            return fetchChartsBatch(showMembers, days);
        }).then(function (items) {
            if (!items || !stillThisCluster(clusterId)) return;
            setStatus('Drawing ' + items.length + ' charts...');
            var drawn = appendDailyCharts(items, clusterId);
            if (!stillThisCluster(clusterId)) return;
            if (withOverlay) paintOverlay(items);
            if (hiddenN > 0) {
                var more = document.createElement('p');
                more.className = 'muted';
                more.textContent = 'Showing first ' + showMembers.length + ' of ' + members.length;
                document.getElementById('chartsRoot').appendChild(more);
            }
            setStatus('Done: ' + drawn + '/' + items.length + (withOverlay ? ' + overlay' : ''));
        }).catch(function (e) {
            if (!stillThisCluster(clusterId)) return;
            setStatus('Charts error: ' + (e && e.message ? e.message : String(e)));
        });
    }

    function simPct() {
        var el = document.getElementById('simSlider');
        return Number(el && el.value) || 88;
    }

    function distanceFromSim(pct) {
        var d = 1 - (pct / 100);
        return Math.max(0.02, Math.min(0.5, Math.round(d * 1000) / 1000));
    }

    function syncSimLabel() {
        var el = document.getElementById('simVal');
        if (el) el.textContent = simPct() + '%';
    }

    function loadBoard(forceRefresh) {
        var head = document.getElementById('detailHead');
        var lookbackEl = document.getElementById('lookbackSel');
        var lookback = lookbackEl ? lookbackEl.value : '126';
        var dist = distanceFromSim(simPct());
        var seq = ++boardLoadSeq;
        currentClusterId = null;
        clearChartsRoot();
        if (head) head.style.display = 'none';
        startHeartbeat(forceRefresh ? 'DB refresh...' : 'Loading map...');

        var q = '/api/portfolio/shape-clusters?lookback_days=' + encodeURIComponent(lookback) +
            '&max_clusters=0' +
            '&distance_threshold=' + encodeURIComponent(String(dist)) +
            '&method=hierarchical&mode=shape&include_overlay=0';
        if (forceRefresh) q += '&refresh=1';

        function attempt(n, lastErr) {
            if (seq !== boardLoadSeq) return Promise.resolve();
            if (n >= 3) {
                stopHeartbeat();
                var em = lastErr && lastErr.message ? String(lastErr.message) : String(lastErr || '');
                setStatus(em.indexOf('timeout') >= 0
                    ? 'Map timeout. Press DB refresh.'
                    : ('Map error: ' + em));
                return Promise.resolve();
            }
            if (n > 0) startHeartbeat('Map retry ' + (n + 1) + '/3...');
            return fetchJsonTimeout(q, forceRefresh ? 60000 : 25000).then(function (r) {
                if (seq !== boardLoadSeq) return null;
                if (!r.ok) {
                    return r.json().catch(function () { return {}; }).then(function (errBody) {
                        throw new Error(errBody.detail || ('HTTP ' + r.status));
                    });
                }
                return r.json();
            }).then(function (report) {
                if (!report || seq !== boardLoadSeq) return;
                stopHeartbeat();
                lastReport = report;
                renderBoard(report.clusters || []);
                var simShow = Math.round((1 - Number(report.distance_threshold || dist)) * 100);
                var meta = document.getElementById('metaLine');
                if (meta) {
                    meta.textContent =
                        'ok=' + (report.n_tickers_ok || 0) + '/' + (report.n_tickers_requested || 0) +
                        ' groups=' + (report.n_clusters || 0) +
                        ' thr~' + simShow + '%' +
                        ' cache=' + (report.cache_source || (report.cache_hit ? 'yes' : 'live'));
                }
                setStatus('Map ready - click a cluster');
            }).catch(function (e) {
                return attempt(n + 1, e);
            });
        }

        return attempt(0, null);
    }

    function scheduleBoardReload() {
        syncSimLabel();
        if (simDebounce) clearTimeout(simDebounce);
        simDebounce = setTimeout(function () { loadBoard(false); }, 350);
    }

    function wireControls() {
        var reloadBtn = document.getElementById('reloadBtn');
        var lookbackSel = document.getElementById('lookbackSel');
        var simSlider = document.getElementById('simSlider');
        if (reloadBtn) reloadBtn.addEventListener('click', function () { loadBoard(true); });
        if (lookbackSel) lookbackSel.addEventListener('change', function () { loadBoard(false); });
        if (simSlider) {
            simSlider.addEventListener('input', syncSimLabel);
            simSlider.addEventListener('change', scheduleBoardReload);
        }
    }

    try {
        setStatus('JS OK - starting map...');
        wireControls();
        syncSimLabel();
        if (typeof Promise === 'undefined' || typeof fetch === 'undefined') {
            setStatus('Browser too old: need fetch+Promise');
            return;
        }
        loadBoard(false);
    } catch (e) {
        setStatus('Boot error: ' + (e && e.message ? e.message : String(e)));
    }
})();
