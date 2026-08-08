window.addEventListener('DOMContentLoaded', function () {

    // ============================== state ==================================
    let cy = null;                 // cytoscape instance for the drawn subgraph
    let nodeID = {};               // gene label -> cytoscape selector
    let allTooltips = {};
    let inputNode = null;          // gene the current graph is centred on
    // Gene that auto-applied filter changes should re-query. Distinct from
    // inputNode: a gene that is not in the graph must not become the standing
    // target, or every later slider nudge re-asks for a gene we know is absent.
    let resolvedGene = null;
    let completeData = null;       // full server response, before the top-N trim
    let requestSeq = 0;            // guards against out-of-order auto-apply responses

    // Landing-view payload: genes ranked by how many samples carry them on ecDNA.
    // Independent of the edge filters, so it is fetched once.
    let overview = null;
    // The top-N the user asked for, kept apart from the value the controls show.
    // A filter that leaves fewer partners clamps the controls; without this, the
    // larger limit would be lost and not come back when the filter is loosened.
    let desiredLimit = null;
    // Bumped whenever completeData is replaced, so the graph/table/chart trio can
    // tell a genuine change from a tab switch and skip a needless relayout.
    let dataToken = 0;
    let dataSignature = null;
    // The view a too-tight filter bounced the reader off, so loosening it returns.
    let viewBeforeEmpty = null;

    // Column indices in #data-table. These match the data-col attributes in the
    // template; sorting is bound by that attribute, not by DOM order, because the
    // header spans two rows.
    const COL = {
        gene: 0, oncogene: 1, location: 2, amps: 3, coamps: 4, freq: 5, distance: 6,
        si_p: 7, si_q: 8, si_or: 9,
        mi_p: 10, mi_q: 11, mi_or: 12,
        mc_p: 13, mc_q: 14, mc_or: 15
    };

    // Anything left at these values is "not a filter the user set", so its control
    // is not highlighted as modified.
    const DEFAULTS = {
        numSamples: 1,
        edgeWeight: 0.1,
        oncogenes_only: false,
        limit: 50,
        rankBy: 'weight',
        sigThreshold: 0.05,
        sigTest: 'any'
    };

    const RANK_LABELS = {
        weight: 'co-amplification frequency',
        leninter: 'number of co-amplifications',
        amps: 'amplification count'
    };

    const TEST_LABELS = {
        any: 'any test',
        single_interval: 'single interval',
        multi_interval: 'multi interval',
        multi_chromosomal: 'multi chromosomal'
    };

    const COLOR_ONCOGENE = '#ff4757';
    const COLOR_GENE = '#a7c6ed';

    const el = {
        search: document.getElementById('geneSearch'),
        searchButton: document.getElementById('searchButton'),
        searchField: document.querySelector('.search-field'),
        searchError: document.getElementById('searchError'),
        busy: document.getElementById('busyIndicator'),
        numSamples: document.getElementById('numSamples'),
        edgeWeight: document.getElementById('edgeWeight'),
        edgeWeightRange: document.getElementById('edgeWeightRange'),
        cutoffNote: document.getElementById('cutoffNote'),
        oncogenes: document.getElementById('oncogenes_only'),
        limit: document.getElementById('limit'),
        limitRange: document.getElementById('limitRange'),
        limitNote: document.getElementById('limitNote'),
        rankBy: document.getElementById('rankBy'),
        sigThreshold: document.getElementById('sigThreshold'),
        sigTest: document.getElementById('sigTest'),
        resetFilters: document.getElementById('resetFilters'),
        legendSig: document.getElementById('legendSig'),
        locationHeader: document.getElementById('locationHeader'),
        summaryTotals: document.getElementById('summaryTotals'),
        projectList: document.getElementById('projectList'),
        summaryGenes: document.getElementById('summaryGenes'),
        summaryRef: document.getElementById('summaryRef'),
        geneHeader: document.getElementById('geneHeader'),
        ghName: document.getElementById('ghName'),
        ghType: document.getElementById('ghType'),
        ghLocation: document.getElementById('ghLocation'),
        ghSamples: document.getElementById('ghSamples'),
        ghProjects: document.getElementById('ghProjects'),
        overviewView: document.getElementById('overviewView'),
        overviewNote: document.getElementById('overviewNote'),
        overviewChart: document.getElementById('overviewChart'),
        overviewEmpty: document.getElementById('overviewEmpty'),
        overviewScope: document.getElementById('overviewScope'),
        viewBar: document.getElementById('viewBar'),
        viewCount: document.getElementById('viewCount'),
        graphView: document.getElementById('graphView'),
        tableView: document.getElementById('tableView'),
        chart: document.getElementById('chartPartners'),
        chartMetricLabel: document.getElementById('chartMetricLabel'),
        dataContainer: document.getElementById('data-container'),
        downloadCurrent: document.getElementById('download-btn'),
        downloadSvg: document.getElementById('download-svg-btn'),
        downloadAll: document.getElementById('download-full-csv-btn')
    };

    cytoscape.use(cytoscapePopper(tippyFactory));

    try {
        cytoscape.use(cytoscapeSvg);
    } catch (e) {
        console.error('Failed to load cytoscape-svg:', e);
    }

    const projectStats = JSON.parse(document.getElementById('project-stats-data').textContent);
    const projectNames = Object.keys(projectStats);

    const refGenomesEl = document.getElementById('reference-genomes-data');
    const refGenomes = refGenomesEl ? JSON.parse(refGenomesEl.textContent) : [];
    const refLabel = refGenomes.length ? refGenomes.join(', ') : 'unknown reference';

    const nf = (n) => Number(n).toLocaleString();

    // What the graph was built from. Restored to the landing view: it is the only
    // place the scope of the query is stated before a gene is chosen.
    (function renderProjectSummary() {
        let totalSamples = 0;
        let totalEcdna = 0;

        el.projectList.innerHTML = '';
        projectNames.forEach(name => {
            const [samples, ecdna] = projectStats[name];
            totalSamples += samples;
            totalEcdna += ecdna;

            const li = document.createElement('li');
            const label = document.createElement('span');
            label.className = 'project-name';
            label.textContent = name;
            const counts = document.createElement('span');
            counts.className = 'project-counts';
            counts.textContent = `${nf(samples)} sample${samples === 1 ? '' : 's'} · ` +
                                 `${nf(ecdna)} ecDNA feature${ecdna === 1 ? '' : 's'}`;
            li.appendChild(label);
            li.appendChild(counts);
            el.projectList.appendChild(li);
        });

        el.summaryTotals.innerHTML =
            `<span class="figure">${nf(projectNames.length)}</span> project` +
            `${projectNames.length === 1 ? '' : 's'} · ` +
            `<span class="figure">${nf(totalSamples)}</span> sample` +
            `${totalSamples === 1 ? '' : 's'} · ` +
            `<span class="figure">${nf(totalEcdna)}</span> ecDNA feature` +
            `${totalEcdna === 1 ? '' : 's'}`;

        el.summaryRef.textContent = `Reference genome: ${refLabel}`;
    })();

    el.locationHeader.title = `Gene coordinates on ${refLabel}`;

    // ============================ filter state =============================

    function num(input, fallback) {
        const v = parseFloat(input.value);
        return isNaN(v) ? fallback : v;
    }

    function currentFilters() {
        return {
            numSamples: num(el.numSamples, DEFAULTS.numSamples),
            edgeWeight: num(el.edgeWeight, DEFAULTS.edgeWeight),
            oncogenes_only: el.oncogenes.checked,
            limit: Math.max(1, Math.round(num(el.limit, DEFAULTS.limit))),
            rankBy: el.rankBy.value,
            sigThreshold: num(el.sigThreshold, DEFAULTS.sigThreshold),
            sigTest: el.sigTest.value
        };
    }

    // A half-typed number ("", "0.") should not fire a query. Only the two server
    // parameters are checked here: an unusable top-N or significance threshold is
    // a client-side concern and must not block a refetch.
    function serverFiltersAreValid() {
        const samples = parseFloat(el.numSamples.value);
        const weight = parseFloat(el.edgeWeight.value);
        return !isNaN(samples) && samples >= 1
            && !isNaN(weight) && weight >= 0 && weight <= 1;
    }

    function limitIsValid() {
        const limit = parseFloat(el.limit.value);
        return !isNaN(limit) && limit >= 1;
    }

    // The filter rail is always visible, so there are no chips to summarise it.
    // Controls sitting off their default get a filled pill instead, which reads at
    // a glance without duplicating the control elsewhere.
    function refreshFilterAffordances() {
        const f = currentFilters();

        el.numSamples.classList.toggle('is-modified', f.numSamples !== DEFAULTS.numSamples);
        el.edgeWeight.classList.toggle('is-modified', f.edgeWeight !== DEFAULTS.edgeWeight);
        // What the user asked for, not what the population let them have: a limit
        // of 50 clamped to the 12 partners that exist is still the default.
        const limitIntent = desiredLimit === null ? f.limit : desiredLimit;
        el.limit.classList.toggle('is-modified', limitIntent !== DEFAULTS.limit);
        el.rankBy.classList.toggle('is-modified', f.rankBy !== DEFAULTS.rankBy);
        el.sigThreshold.classList.toggle('is-modified', f.sigThreshold !== DEFAULTS.sigThreshold);
        el.sigTest.classList.toggle('is-modified', f.sigTest !== DEFAULTS.sigTest);

        // The legend states the threshold the orange edges actually reflect, so the
        // colour is never unexplained.
        el.legendSig.textContent = `(q \u2264 ${f.sigThreshold}, ${TEST_LABELS[f.sigTest]})`;

        el.cutoffNote.textContent = cutoffSentence(f);
    }

    // The two cutoffs are not independent: the frequency is a fraction of the
    // union, so requiring a union of N samples plus a frequency of F already
    // demands ceil(F*N) shared samples. Spelling that number out is the only way
    // the dependency is visible while the controls are being moved.
    function cutoffSentence(f) {
        const shared = Math.max(1, Math.ceil(f.edgeWeight * f.numSamples));
        return `Both must pass: at least ${f.numSamples} sample${f.numSamples === 1 ? '' : 's'} ` +
               `carry either gene, and at least ${f.edgeWeight} of those carry both ` +
               `\u2014 so ${shared} shared sample${shared === 1 ? '' : 's'} at minimum.`;
    }

    // ---- linked slider/box pairs ------------------------------------------
    // Each pair has exactly one writer. Assigning both inputs independently let
    // them drift whenever the range clamped a value the box did not.

    function setEdgeWeight(value) {
        if (!isFinite(value)) { return; }
        el.edgeWeightRange.value = String(Math.min(1, Math.max(0, value)));
        el.edgeWeight.value = Number(el.edgeWeightRange.value).toFixed(2);
    }

    // The range element is the authority: it clamps whatever it is given to
    // [min, max], so reading its value back is what the box must show.
    function setLimit(value) {
        if (!isFinite(value)) { return; }
        el.limitRange.value = String(Math.max(1, Math.round(value)));
        // Only write when it actually differs - rewriting on every keystroke
        // would move the caret to the end mid-edit.
        if (el.limit.value !== el.limitRange.value) {
            el.limit.value = el.limitRange.value;
        }
    }

    el.edgeWeightRange.addEventListener('input', () => {
        setEdgeWeight(Number(el.edgeWeightRange.value));
        scheduleRefetch();
    });

    el.limitRange.addEventListener('input', () => {
        desiredLimit = Number(el.limitRange.value);
        setLimit(desiredLimit);
        scheduleRerender();
    });

    el.resetFilters.addEventListener('click', () => {
        el.numSamples.value = DEFAULTS.numSamples;
        setEdgeWeight(DEFAULTS.edgeWeight);
        el.oncogenes.checked = DEFAULTS.oncogenes_only;
        desiredLimit = DEFAULTS.limit;
        setLimit(DEFAULTS.limit);
        el.rankBy.value = DEFAULTS.rankBy;
        el.sigThreshold.value = DEFAULTS.sigThreshold;
        el.sigTest.value = DEFAULTS.sigTest;

        refreshFilterAffordances();
        applySignificance();
        scheduleRefetch();
        scheduleRerender();
    });

    // ========================= Neo4j interaction ===========================

    function setBusy(state) {
        el.busy.hidden = !state;
    }

    function showError(message) {
        el.searchError.textContent = message;
        el.searchError.hidden = false;
        el.searchField.classList.add('has-error');
    }

    function clearError() {
        el.searchError.hidden = true;
        el.searchField.classList.remove('has-error');
    }

    /**
     * Query the subgraph centred on `gene`.
     *
     * `isRefilter` distinguishes the two ways an empty result can happen: a gene
     * that is not in the graph at all, versus filters that excluded everything
     * around a gene we have already resolved. The server cannot tell us which,
     * because a gene with no surviving edges is simply absent from the result.
     */
    async function fetchSubgraph(gene, { isRefilter = false } = {}) {
        if (!gene) { return; }
        if (!serverFiltersAreValid()) { return; }

        const seq = ++requestSeq;
        const f = currentFilters();
        setBusy(true);

        const params = new URLSearchParams({
            min_weight: f.edgeWeight,
            min_samples: f.numSamples,
            oncogenes: f.oncogenes_only,
            all_edges: false
        });

        try {
            const response = await fetch(
                `/coamplification-graph/visualizer/${encodeURIComponent(gene)}/?${params.toString()}`
            );

            // A newer request has already been issued - drop this one rather than
            // letting a slow early response overwrite a fast later one.
            if (seq !== requestSeq) { return; }

            if (!response.ok) {
                let detail = `Server error (${response.status}).`;
                try {
                    const body = await response.json();
                    if (body && body.error) { detail = body.error; }
                } catch (e) { /* non-JSON error body */ }
                throw new Error(detail);
            }

            const data = await response.json();
            if (seq !== requestSeq) { return; }

            if (!data.nodes || data.nodes.length === 0) {
                if (isRefilter) {
                    // The gene is real, the filters are just too tight - keep it as
                    // the target so loosening a filter brings the graph back.
                    showError('No co-amplifications pass the current filters.');
                } else {
                    showError('Gene not found in graph. Please try again.');
                    resolvedGene = null;
                }
                // Do not leave a graph on screen that the filters exclude.
                completeData = data.nodes ? data : { nodes: [], edges: [] };
                inputNode = gene;
                dataToken++;
                renderAll();
                return;
            }

            clearError();
            completeData = data;
            inputNode = gene;
            resolvedGene = gene;
            dataToken++;
            // A fresh search lands on the graph; a filter nudge leaves the user on
            // whichever view they were reading, Overview included.
            if (!isRefilter) {
                setActiveTab('graph');
                viewBeforeEmpty = null;
            }
            renderAll();
        } catch (error) {
            if (seq === requestSeq) { showError(error.message); }
        } finally {
            if (seq === requestSeq) { setBusy(false); }
        }
    }

    // ---- auto-apply -------------------------------------------------------
    // Server-side filters need a round trip; the top-N trim and the ranking key
    // only reshape data we already hold. Both are debounced so dragging or typing
    // does not fire a request (or a relayout) per keystroke.
    let refetchTimer = null;
    let rerenderTimer = null;

    function scheduleRefetch() {
        refreshFilterAffordances();
        if (!resolvedGene) { return; }
        clearTimeout(refetchTimer);
        refetchTimer = setTimeout(() => fetchSubgraph(resolvedGene, { isRefilter: true }), 400);
    }

    // The overview is redrawn here too: it responds to the top-N trim and to the
    // oncogene toggle, and on the landing page there is no completeData to gate on.
    function scheduleRerender() {
        refreshFilterAffordances();
        if (!limitIsValid()) { return; }
        clearTimeout(rerenderTimer);
        rerenderTimer = setTimeout(() => {
            if (activeView() === 'overview') {
                updateLimitBounds();
                renderOverview();
                renderCounts();
            } else if (completeData) {
                renderAll();
            }
        }, 250);
    }

    ['input', 'change'].forEach(evt => {
        el.numSamples.addEventListener(evt, scheduleRefetch);
        el.edgeWeight.addEventListener(evt, () => {
            // Mirror into the slider but leave the box alone while it is being
            // typed into; setEdgeWeight would reformat "0.1" to "0.10" mid-keystroke.
            const v = parseFloat(el.edgeWeight.value);
            if (!isNaN(v)) { el.edgeWeightRange.value = String(Math.min(1, Math.max(0, v))); }
            scheduleRefetch();
        });
        el.limit.addEventListener(evt, () => {
            const v = parseFloat(el.limit.value);
            if (isNaN(v)) { return; }   // mid-edit; leave the pair as it stands
            desiredLimit = Math.max(1, Math.round(v));
            setLimit(desiredLimit);
            scheduleRerender();
        });
    });
    // The overview chart honours this too, so it needs a re-render either way.
    el.oncogenes.addEventListener('change', () => {
        scheduleRefetch();
        scheduleRerender();
    });
    el.rankBy.addEventListener('change', scheduleRerender);

    el.sigThreshold.addEventListener('input', () => {
        refreshFilterAffordances();
        applySignificance();
    });
    el.sigTest.addEventListener('change', () => {
        refreshFilterAffordances();
        applySignificance();
    });

    function submitSearch() {
        const gene = el.search.value.trim().toUpperCase();
        if (!gene) {
            showError('Enter a gene name to search.');
            el.search.focus();
            return;
        }
        el.search.value = gene;
        fetchSubgraph(gene);
    }

    el.search.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') { return; }
        event.preventDefault();
        submitSearch();
    });

    el.searchButton.addEventListener('click', submitSearch);

    // ============================= rendering ===============================

    function activeView() {
        const tab = document.querySelector('.tab.is-active');
        return tab ? tab.dataset.view : 'graph';
    }

    function hasGraphData() {
        return !!(completeData && inputNode && completeData.nodes.length > 0);
    }

    function renderAll() {
        if (!completeData || !inputNode) { return; }

        const hasData = hasGraphData();

        setDataTabsEnabled(hasData);
        el.geneHeader.hidden = !hasData;
        el.downloadCurrent.disabled = !hasData;
        el.downloadSvg.disabled = !hasData;

        // The standing instruction lives under the search box, so this slot carries
        // only the outcome of a search and stays out of the way before one is made.
        if (!hasData) {
            el.overviewNote.textContent =
                `No genes to show for ${inputNode} under the current filters.`;
            el.overviewNote.hidden = false;
            // Being dropped here is involuntary, so remember what it interrupted.
            if (activeView() !== 'overview') { viewBeforeEmpty = activeView(); }
            showView('overview');
            return;
        }

        el.overviewNote.textContent = '';
        el.overviewNote.hidden = true;
        // Loosening the filter that emptied the graph puts the reader back where
        // they were, rather than leaving them on the view they were bounced to.
        if (viewBeforeEmpty) {
            setActiveTab(viewBeforeEmpty);
            viewBeforeEmpty = null;
        }
        showView(activeView());
    }

    // The graph, table and partner chart are one unit: they all read the same
    // trimmed slice. Rebuilding them is expensive (a full fcose relayout), so a
    // tab switch that changes nothing about the slice only re-measures.
    function currentDataSignature() {
        const f = currentFilters();
        return `${dataToken}|${f.limit}|${f.rankBy}`;
    }

    function renderDataViews(name) {
        if (!hasGraphData()) { return; }

        const signature = currentDataSignature();
        if (signature !== dataSignature) {
            dataSignature = signature;
            renderGeneHeader();
            renderGraph();
            renderTable();
            renderChart();
            return;
        }

        // Cytoscape and Plotly both measure their container, and both measure
        // zero while hidden. Re-measure on the way back in.
        if (name === 'graph') {
            if (cy) {
                cy.resize();
                cy.fit();
            }
            if (typeof Plotly !== 'undefined' && el.chart.data) {
                Plotly.Plots.resize(el.chart);
            }
        }
    }

    /** Rank an edge by whichever metric the "top N genes by ..." select names. */
    function rankValue(edge, nodeById, rankBy) {
        if (rankBy === 'leninter') {
            return edge.data.leninter || 0;
        }
        if (rankBy === 'amps') {
            const partner = edge.data.source === inputNode ? edge.data.target : edge.data.source;
            const node = nodeById.get(partner);
            return node && node.data.samples ? node.data.samples.length : 0;
        }
        return edge.data.weight || 0;
    }

    function filterData(data, topN, rankBy) {
        const nodeById = new Map(data.nodes.map(n => [n.data.id, n]));

        const sortedEdges = data.edges
            .slice()
            .sort((a, b) => rankValue(b, nodeById, rankBy) - rankValue(a, nodeById, rankBy));

        const topEdges = sortedEdges.slice(0, topN);

        const nodeIds = new Set();
        topEdges.forEach(edge => {
            nodeIds.add(edge.data.source);
            nodeIds.add(edge.data.target);
        });
        // The query gene anchors the view even if every edge was trimmed away.
        nodeIds.add(inputNode);

        return {
            edges: topEdges,
            nodes: data.nodes.filter(node => nodeIds.has(node.data.id))
        };
    }

    function renderGraph() {
        removeAllTooltips();

        cy = null;
        nodeID = {};
        document.getElementById('cy').innerHTML = '';

        const f = currentFilters();
        const shown = filterData(completeData, f.limit, f.rankBy);

        cy = cytoscape({
            container: document.getElementById('cy'),
            elements: shown,
            style: [
                {
                    selector: 'node',
                    style: {
                        'background-color': COLOR_GENE,
                        'label': 'data(label)',
                        'text-valign': 'center',
                        'text-halign': 'center',
                        'color': '#333',
                        'font-size': '12px',
                        'text-outline-width': 2,
                        'text-outline-color': '#fff'
                    }
                },
                {
                    selector: 'node[oncogene="True"]',
                    style: {
                        'background-color': COLOR_ONCOGENE,
                        'z-index': 10,
                        'color': '#fff',
                        'font-weight': 'bold',
                        'text-outline-width': 2,
                        'text-outline-color': COLOR_ONCOGENE
                    }
                },
                {
                    selector: `node[label="${inputNode}"]`,
                    style: {
                        'z-index': 100,
                        'background-color': COLOR_ONCOGENE,
                        'color': '#fff',
                        'font-weight': 'bold',
                        'font-size': '14px',
                        'border-width': 3,
                        'border-color': '#8b1f2a',
                        'text-outline-width': 2,
                        'text-outline-color': COLOR_ONCOGENE
                    }
                },
                { selector: 'edge', style: { 'width': 1, 'line-color': '#b6bcc4' } },
                { selector: 'edge.significant', style: { 'width': 3, 'line-color': '#f5a623' } },
                { selector: '.highlighted', style: { 'z-index': 100, 'background-color': '#ffd500', 'line-color': '#ffd500' } }
            ]
        });

        cy.nodes().forEach(node => {
            nodeID[node.data('label')] = '#' + node.id();
        });

        if (cy.nodes().length > 0) {
            layout(cy, inputNode);
            makeTips(cy);
            cy.ready(() => {
                cy.resize();
                cy.fit();
            });
        }

        cy.on('tap', 'edge', (event) => {
            const edge = event.target;
            const width = Number(edge.style('width').replace('px', ''));
            const scale = 3;
            edge.animate(
                { style: { 'width': edge.hasClass('highlighted') ? width * scale : width / scale } },
                { duration: 300, easing: 'ease-in-out' }
            );
        });

        cy.on('tap', 'node', (event) => {
            const node = event.target;
            const size = node.data('size');
            const scale = 1.3;
            node.animate(
                { style: { 'width': node.hasClass('highlighted') ? size * scale : size / scale,
                           'height': node.hasClass('highlighted') ? size * scale : size / scale } },
                { duration: 300, easing: 'ease-in-out' }
            );
        });

        // A fresh instance starts with no edge classes, so re-apply whatever the
        // significance controls are currently asking for.
        applySignificance();
    }

    function renderGeneHeader() {
        const node = completeData.nodes.find(n => n.data.label === inputNode);
        if (!node) { return; }

        const d = node.data;
        el.ghName.textContent = d.label;
        el.ghType.textContent = d.oncogene === 'True' ? 'Oncogene' : 'Gene';

        el.ghLocation.innerHTML = '';
        if (d.location && d.location.length >= 3) {
            const chrom = String(d.location[0]);
            const prefixed = chrom.toLowerCase().startsWith('chr') ? chrom : `chr${chrom}`;
            el.ghLocation.appendChild(
                document.createTextNode(`${prefixed}: ${nf(d.location[1])}-${nf(d.location[2])}`));
            // Coordinates are meaningless without the assembly they are on.
            const ref = document.createElement('span');
            ref.className = 'qualifier';
            ref.textContent = ` (${refLabel})`;
            el.ghLocation.appendChild(ref);
            el.ghLocation.title = `${prefixed}:${d.location[1]}-${d.location[2]} on ${refLabel}`;
        } else {
            el.ghLocation.textContent = 'N/A';
            el.ghLocation.title = `Reference genome: ${refLabel}`;
        }

        renderTruncatedList(el.ghSamples, d.samples || [], 'sample');

        // The graph is built across exactly the projects that were selected; the
        // node payload carries sample names, not their project of origin, so this
        // is the project set rather than a per-gene breakdown.
        renderTruncatedList(el.ghProjects, projectNames, 'project');
    }

    // Leads with the count: "3 of them" is the number being asked for, and the
    // names are the supporting detail rather than the other way round.
    function renderTruncatedList(target, items, noun) {
        target.innerHTML = '';
        if (items.length === 0) {
            target.textContent = `0 ${noun}s`;
            target.title = '';
            return;
        }

        const total = document.createElement('span');
        total.textContent = `${nf(items.length)} ${noun}${items.length === 1 ? '' : 's'}`;
        target.appendChild(total);

        const names = document.createElement('span');
        names.className = 'more';
        names.textContent = items.length > 2
            ? ` — ${items.slice(0, 2).join(', ')} +${items.length - 2} more`
            : ` — ${items.join(', ')}`;
        target.appendChild(names);

        target.title = `${items.length} ${noun}${items.length === 1 ? '' : 's'}: ${items.join(', ')}`;
    }

    // "Show top genes" means a different population in each view, so its bounds
    // and its readout follow whichever view is on screen.
    function limitPopulation() {
        if (activeView() !== 'overview' && hasGraphData()) {
            return { total: completeData.edges.length, noun: 'co-amplified gene' };
        }
        return {
            total: overviewList().length,
            noun: overviewIsOncogenes() ? 'ranked oncogene' : 'ranked gene'
        };
    }

    // Point the slider at the range that actually exists, then restate the limit
    // the user asked for. Displaying the clamp rather than storing it is what
    // keeps a limit set over a wider population from being quietly lost.
    function updateLimitBounds() {
        const { total, noun } = limitPopulation();

        if (desiredLimit === null) {
            desiredLimit = Math.max(1, Math.round(num(el.limit, DEFAULTS.limit)));
        }

        // A population of zero means "not known yet", not "one gene": clamping to
        // it on the first paint would drag the control down to 1 before the
        // overview has even arrived.
        if (total > 0) {
            el.limitRange.max = String(total);
            el.limit.max = String(total);
        }
        setLimit(desiredLimit);

        el.limitNote.textContent = total
            ? `of ${nf(total)} ${noun}${total === 1 ? '' : 's'}`
            : '';
    }

    function renderCounts() {
        if (activeView() === 'overview') {
            const total = overviewList().length;
            if (!total) { el.viewCount.textContent = ''; return; }
            const shown = Math.min(total, currentFilters().limit);
            const noun = overviewIsOncogenes() ? 'oncogene' : 'gene';
            el.viewCount.textContent =
                `Showing ${shown} of ${nf(total)} most-amplified ${noun}${total === 1 ? '' : 's'}`;
            return;
        }

        if (!cy || !completeData) { el.viewCount.textContent = ''; return; }
        const shown = Math.max(0, cy.nodes().length - 1);
        const total = Math.max(0, completeData.nodes.length - 1);
        el.viewCount.textContent = `Showing ${shown} of ${total} co-amplified gene${total === 1 ? '' : 's'}`;
    }

    // ============================== overview ===============================

    // A handful of oncogenes is not a chart. Below this many, rank every gene
    // instead, so the landing view always has something to say.
    const ONCOGENE_CHART_MIN = 5;

    function overviewIsOncogenes() {
        if (!overview) { return false; }
        if (el.oncogenes.checked) { return overview.oncogene_total > 0; }
        return overview.oncogene_total >= ONCOGENE_CHART_MIN;
    }

    function overviewList() {
        if (!overview) { return []; }
        return (overviewIsOncogenes() ? overview.oncogenes : overview.genes) || [];
    }

    async function fetchOverview() {
        try {
            const response = await fetch('/coamplification-graph/overview/');
            if (!response.ok) { throw new Error(`Server error (${response.status}).`); }
            overview = await response.json();
        } catch (error) {
            console.error('Failed to load the gene overview:', error);
            // Flagged rather than left to look like an empty graph: the two say
            // very different things about whether anything is wrong.
            overview = { genes: [], oncogenes: [], gene_total: 0, oncogene_total: 0, failed: true };
            el.overviewEmpty.textContent = 'Could not load the gene list.';
        }

        renderSummaryGenes();
        if (activeView() === 'overview') {
            updateLimitBounds();
            renderOverview();
            renderCounts();
        }
    }

    function renderSummaryGenes() {
        if (!overview || overview.failed) { return; }
        el.summaryGenes.textContent =
            `${nf(overview.gene_total)} gene${overview.gene_total === 1 ? '' : 's'} on ecDNA · ` +
            `${nf(overview.oncogene_total)} oncogene${overview.oncogene_total === 1 ? '' : 's'}`;
    }

    function renderOverview() {
        const ranked = overviewList();
        el.overviewScope.textContent = overviewIsOncogenes() ? 'oncogenes' : 'genes';

        if (typeof Plotly === 'undefined') { return; }

        if (ranked.length === 0) {
            Plotly.purge(el.overviewChart);
            el.overviewChart.innerHTML = '';
            el.overviewEmpty.hidden = false;
            return;
        }
        el.overviewEmpty.hidden = true;

        // Plotly draws a horizontal bar chart bottom-up, so the top-N slice is
        // reversed to put the most-amplified gene at the top.
        const rows = ranked.slice(0, currentFilters().limit).reverse();

        const trace = {
            type: 'bar',
            orientation: 'h',
            x: rows.map(r => r.amps),
            y: rows.map(r => r.label),
            marker: { color: rows.map(r => (r.oncogene ? COLOR_ONCOGENE : COLOR_GENE)) },
            hovertemplate:
                '<b>%{y}</b><br>Amplified on ecDNA: %{x} sample(s)' +
                '<br><i>click to build its graph</i><extra></extra>'
        };

        const layoutOpts = {
            height: Math.max(220, rows.length * 24 + 70),
            margin: { l: 96, r: 24, t: 46, b: 12 },
            xaxis: {
                side: 'top',
                title: { text: 'samples with the gene on ecDNA', font: { size: 11 }, standoff: 6 },
                zeroline: false,
                gridcolor: '#eef1f4',
                tickfont: { size: 11 }
            },
            yaxis: { automargin: true, tickfont: { size: 11 } },
            bargap: 0.28,
            hoverlabel: { align: 'left' },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { family: "'Helvetica Neue', Arial, sans-serif", color: '#1f2933' }
        };

        Plotly.react(el.overviewChart, [trace], layoutOpts, {
            displayModeBar: false,
            responsive: true
        });

        // The chart doubles as the gene picker: no one arrives here knowing which
        // gene to type.
        el.overviewChart.removeAllListeners && el.overviewChart.removeAllListeners('plotly_click');
        el.overviewChart.on('plotly_click', (event) => {
            const point = event.points && event.points[0];
            if (!point) { return; }
            el.search.value = point.y;
            submitSearch();
        });
    }

    // -1 is the graph's sentinel for "not computed"; the query gene has no edge to
    // itself, so its statistics cells are absent entirely.
    function stat(value) {
        const n = parseFloat(value);
        return (value === -1 || value === undefined || value === null || isNaN(n)) ? 'N/A' : n.toFixed(3);
    }

    function renderTable() {
        el.dataContainer.innerHTML = '';

        if (cy.nodes().length === 0) {
            const row = document.createElement('tr');
            row.className = 'no-data';
            const cell = document.createElement('td');
            cell.colSpan = 16;
            cell.textContent = 'No data available';
            row.appendChild(cell);
            el.dataContainer.appendChild(row);
            return;
        }

        cy.nodes().forEach(node => {
            const geneName = node.data('label');
            const isQuery = geneName === inputNode;
            const edge = node.edgesWith(cy.$(nodeID[inputNode]))[0];
            const e = edge ? edge.data() : {};
            const samples = node.data('samples');
            const location = node.data('location');

            const row = document.createElement('tr');
            if (isQuery) { row.classList.add('query-row'); }

            const geneCell = document.createElement('td');
            const link = document.createElement('a');
            link.href = `https://depmap.org/portal/gene/${encodeURIComponent(geneName)}?tab=overview`;
            link.textContent = geneName;
            link.target = '_blank';
            link.rel = 'noopener';
            geneCell.appendChild(link);
            row.appendChild(geneCell);

            addCell(row, node.data('oncogene'));
            addCell(row, location && location.length >= 3
                ? `${location[0]}:${location[1]}-${location[2]}`
                : 'N/A');
            addCell(row, samples ? String(samples.length) : 'N/A', true);
            // The query gene has no edge to itself, so a dash rather than a zero.
            addCell(row, isQuery ? '—' : String(e.leninter != null ? e.leninter : 'N/A'), true);
            addCell(row, isQuery ? '—' : stat(e.weight), true);
            addCell(row, isQuery || e.distance === -1 || e.distance == null
                ? 'N/A'
                : String(e.distance), true);

            ['single_interval', 'multi_interval', 'multi_chromosomal'].forEach(test => {
                addCell(row, isQuery ? 'N/A' : stat(e[`pval_${test}`]), true);
                addCell(row, isQuery ? 'N/A' : stat(e[`qval_${test}`]), true);
                addCell(row, isQuery ? 'N/A' : stat(e[`odds_ratio_${test}`]), true);
            });

            row.addEventListener('click', () => {
                const target = cy.$(nodeID[geneName]);
                if (target) { target.emit('tap'); }
                row.classList.toggle('active');
            });

            el.dataContainer.appendChild(row);
        });

        sortTable(COL.freq, 'desc');
    }

    function addCell(row, text, numeric) {
        const cell = document.createElement('td');
        cell.textContent = text;
        if (numeric) { cell.className = 'num'; }
        row.appendChild(cell);
        return cell;
    }

    function renderChart() {
        if (typeof Plotly === 'undefined') { return; }

        const f = currentFilters();
        el.chartMetricLabel.textContent = RANK_LABELS[f.rankBy];

        const rows = [];
        cy.nodes().forEach(node => {
            const geneName = node.data('label');
            if (geneName === inputNode) { return; }

            const edge = node.edgesWith(cy.$(nodeID[inputNode]))[0];
            if (!edge) { return; }

            let value;
            if (f.rankBy === 'leninter') {
                value = edge.data('leninter') || 0;
            } else if (f.rankBy === 'amps') {
                const samples = node.data('samples');
                value = samples ? samples.length : 0;
            } else {
                value = edge.data('weight') || 0;
            }

            const samples = node.data('samples');
            rows.push({
                gene: geneName,
                value: value,
                // Hover always reports all three, whatever the bars are ranked by:
                // the ranking metric alone rarely answers the question being asked.
                freq: edge.data('weight') || 0,
                coamps: edge.data('leninter') || 0,
                amps: samples ? samples.length : 0,
                oncogene: node.data('oncogene') === 'True'
            });
        });

        if (rows.length === 0) {
            Plotly.purge(el.chart);
            el.chart.innerHTML = '';
            return;
        }

        // Plotly draws a horizontal bar chart bottom-up, so ascending order here
        // puts the strongest partner at the top.
        rows.sort((a, b) => a.value - b.value);

        const trace = {
            type: 'bar',
            orientation: 'h',
            x: rows.map(r => r.value),
            y: rows.map(r => r.gene),
            customdata: rows.map(r => [r.freq, r.coamps, r.amps]),
            marker: { color: rows.map(r => (r.oncogene ? COLOR_ONCOGENE : COLOR_GENE)) },
            hovertemplate:
                '<b>%{y}</b><br>' +
                'Co-amp frequency: %{customdata[0]:.3f}<br>' +
                'Co-amplified with query: %{customdata[1]} sample(s)<br>' +
                'Amplified on ecDNA: %{customdata[2]} sample(s)' +
                '<extra></extra>'
        };

        const layoutOpts = {
            height: Math.max(220, rows.length * 24 + 70),
            // Ticks sit on top: the panel scrolls vertically, and an axis at the
            // bottom of a 50-bar chart is off-screen exactly when it is needed.
            margin: { l: 96, r: 24, t: 46, b: 12 },
            xaxis: {
                side: 'top',
                title: { text: RANK_LABELS[f.rankBy], font: { size: 11 }, standoff: 6 },
                zeroline: false,
                gridcolor: '#eef1f4',
                tickfont: { size: 11 }
            },
            yaxis: {
                automargin: true,
                tickfont: { size: 11 }
            },
            bargap: 0.28,
            hoverlabel: { align: 'left' },
            paper_bgcolor: 'rgba(0,0,0,0)',
            plot_bgcolor: 'rgba(0,0,0,0)',
            font: { family: "'Helvetica Neue', Arial, sans-serif", color: '#1f2933' }
        };

        Plotly.react(el.chart, [trace], layoutOpts, {
            displayModeBar: false,
            responsive: true
        });

        // Clicking a bar selects the same gene in the graph.
        el.chart.removeAllListeners && el.chart.removeAllListeners('plotly_click');
        el.chart.on('plotly_click', (event) => {
            const point = event.points && event.points[0];
            if (!point) { return; }
            const target = cy.$(nodeID[point.y]);
            if (target && target.length) { target.emit('tap'); }
        });
    }

    // ============================== tooltips ===============================

    function tippyFactory(ref, content, theme) {
        const dummyDomEle = document.createElement('div');
        return tippy(dummyDomEle, {
            getReferenceClientRect: ref.getBoundingClientRect,
            trigger: 'manual',
            content: content,
            arrow: false,
            placement: 'bottom-end',
            hideOnClick: false,
            sticky: 'reference',
            theme: theme,
            allowHTML: true,
            interactive: true,
            appendTo: document.body
        });
    }

    function createTooltipContent(ele) {
        if (ele.isNode()) {
            const template = document.getElementById('node-template');
            const samples = ele.data('samples') || [];
            template.querySelector('#ntip-name').textContent = ele.data('label') || 'N/A';
            template.querySelector('#ntip-location').textContent = ele.data('location')
                ? `${ele.data('location')[0]}:${ele.data('location')[1]}-${ele.data('location')[2]}`
                : 'N/A';
            template.querySelector('#ntip-oncogene').textContent = ele.data('oncogene') || 'N/A';
            template.querySelector('#ntip-nsamples').textContent = samples.length || 'N/A';
            template.querySelector('#ntip-samples').textContent = samples.join(', ') || 'N/A';
            return template.innerHTML;
        }

        const template = document.getElementById('edge-template');
        template.querySelector('#etip-name').textContent = ele.data('label') || 'N/A';
        template.querySelector('#etip-weight').textContent = stat(ele.data('weight'));
        template.querySelector('#etip-frac').textContent = `${ele.data('leninter')}/${ele.data('lenunion')}`;
        template.querySelector('#etip-distance').textContent =
            ele.data('distance') < 0 ? 'N/A' : ele.data('distance');

        ['single_interval', 'multi_interval', 'multi_chromosomal'].forEach(test => {
            template.querySelector(`#etip-pval-${test}`).textContent = stat(ele.data(`pval_${test}`));
            template.querySelector(`#etip-qval-${test}`).textContent = stat(ele.data(`qval_${test}`));
            template.querySelector(`#etip-odds_ratio-${test}`).textContent = stat(ele.data(`odds_ratio_${test}`));
        });

        template.querySelector('#etip-nsamples').textContent = ele.data('leninter') || 'N/A';
        template.querySelector('#etip-samples').textContent = (ele.data('inter') || []).join(', ') || 'N/A';
        return template.innerHTML;
    }

    function makeTips(cyInstance) {
        if (!cyInstance) { return; }
        cyInstance.ready(() => {
            cyInstance.elements().forEach((ele) => {
                const theme = ele.isNode() ? 'node' : 'edge';
                const tooltip = tippyFactory(ele.popperRef(), createTooltipContent(ele), theme);

                tooltip.popper.addEventListener('click', (event) => {
                    if (event.target.classList.contains('tooltip-close-btn')) {
                        ele.removeClass('highlighted');
                        tooltip.hide();
                        event.stopPropagation();
                    }
                });

                ele.on('tap', () => {
                    ele.toggleClass('highlighted');
                    tooltip.state.isVisible ? tooltip.hide() : tooltip.show();
                });

                allTooltips[ele.id()] = tooltip;
            });
        });
    }

    function removeAllTooltips() {
        Object.values(allTooltips).forEach(tooltip => {
            tooltip.hide();
            tooltip.destroy();
        });
        allTooltips = {};
    }

    // ============================== layout =================================

    function layout(cyInstance, input) {
        if (!cyInstance) { return; }
        const radius = 40;

        cyInstance.nodes().forEach(node => {
            let size;
            if (node.data('label') === input) {
                size = radius * 1.5;
            } else {
                const edges = node.edgesWith(cyInstance.$(nodeID[input]));
                const scale = edges.reduce((sum, edge) => sum + edge.data('weight'), 0);
                size = radius * (0.8 + scale);
            }
            node.style({ 'width': size, 'height': size });
            node.data('size', size);
        });

        cyInstance.layout({
            name: 'fcose',
            animate: true,
            animationDuration: 800,
            fit: true,
            padding: 30,
            gravity: 1.5,
            gravityRange: 1.2,
            idealEdgeLength: (edge) => {
                const sourceSize = edge.source().data('size');
                const targetSize = edge.target().data('size');
                return 100 - Math.min(sourceSize, targetSize) * 0.5;
            },
            nodeRepulsion: (node) => 4500 - node.data('size') * 50
        }).run();

        setTimeout(() => {
            cyInstance.fit();
            cyInstance.center();
        }, 500);
    }

    // ============================ significance =============================

    // Re-colours edges for the current test and threshold. Bound once, not per
    // graph load - the old code stacked a duplicate listener on every draw.
    function applySignificance() {
        if (!cy) { return; }

        const threshold = num(el.sigThreshold, DEFAULTS.sigThreshold);
        const selectedTest = el.sigTest.value;

        cy.edges().forEach(edge => {
            let isSignificant = false;

            if (selectedTest === 'any') {
                const qvals = [
                    parseFloat(edge.data('qval_single_interval')),
                    parseFloat(edge.data('qval_multi_interval')),
                    parseFloat(edge.data('qval_multi_chromosomal'))
                ];
                isSignificant = qvals.some(q => !isNaN(q) && q >= 0 && q <= threshold);
            } else {
                const qval = parseFloat(edge.data(`qval_${selectedTest}`));
                isSignificant = !isNaN(qval) && qval >= 0 && qval <= threshold;
            }

            edge.toggleClass('significant', isSignificant);
        });
    }

    // ============================ view switching ===========================

    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => {
            if (tab.disabled) { return; }
            showView(tab.dataset.view);
        });
    });

    function setActiveTab(name) {
        document.querySelectorAll('.tab').forEach(tab => {
            const active = tab.dataset.view === name;
            tab.classList.toggle('is-active', active);
            tab.setAttribute('aria-selected', String(active));
        });
    }

    // Graph and Table have nothing to show until a search resolves, but the bar
    // is on screen from the first paint so that Overview reads as a tab.
    function setDataTabsEnabled(enabled) {
        document.querySelectorAll('.tab').forEach(tab => {
            if (tab.dataset.view === 'overview') { return; }
            tab.disabled = !enabled;
        });
    }

    function showView(name) {
        setActiveTab(name);

        // Unhide before drawing: Cytoscape and Plotly both size themselves from
        // their container, and a display:none container measures zero.
        el.overviewView.hidden = name !== 'overview';
        el.graphView.hidden = name !== 'graph';
        el.tableView.hidden = name !== 'table';

        // The top-N control counts a different population per view, so its bounds
        // are re-derived before anything is drawn against them.
        updateLimitBounds();

        if (name === 'overview') {
            renderOverview();
        } else {
            renderDataViews(name);
        }

        renderCounts();
    }

    // ============================ table sorting ============================

    function sortableHeaders() {
        return Array.from(document.querySelectorAll('#data-table th[data-col]'));
    }

    function sortTable(columnIndex, defaultOrder = null) {
        const table = document.getElementById('data-table');
        const tbody = el.dataContainer;
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const noDataRow = tbody.querySelector('.no-data');
        if (noDataRow) { return; }

        const sortOrder = defaultOrder
            || (table.dataset.sortOrder === 'asc' && Number(table.dataset.sortCol) === columnIndex
                ? 'desc'
                : (Number(table.dataset.sortCol) === columnIndex ? 'asc' : 'desc'));

        table.dataset.sortOrder = sortOrder;
        table.dataset.sortCol = String(columnIndex);

        // The query gene has no edge to itself, so its numeric cells read 'N/A'.
        // Pin it to the top instead of letting it sort as text among the numbers -
        // that made the comparator inconsistent. generateCSV() does the same.
        const queryRows = rows.filter(r => r.classList.contains('query-row'));
        const sortable = rows.filter(r => !r.classList.contains('query-row'));

        sortable.sort((a, b) => {
            const cellA = a.children[columnIndex].textContent.trim();
            const cellB = b.children[columnIndex].textContent.trim();
            const numA = parseFloat(cellA);
            const numB = parseFloat(cellB);
            const aIsNum = cellA !== '' && !isNaN(numA);
            const bIsNum = cellB !== '' && !isNaN(numB);

            if (aIsNum && bIsNum) {
                return sortOrder === 'asc' ? numA - numB : numB - numA;
            }
            if (aIsNum !== bIsNum) {
                // Values without a number sort after those with one in both
                // directions, so the comparator stays transitive.
                return aIsNum ? -1 : 1;
            }
            return sortOrder === 'asc'
                ? cellA.localeCompare(cellB)
                : cellB.localeCompare(cellA);
        });

        [...queryRows, ...sortable].forEach(row => tbody.appendChild(row));
        updateSortIndicators(columnIndex, sortOrder);
    }

    function updateSortIndicators(columnIndex, sortOrder) {
        sortableHeaders().forEach(th => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (Number(th.dataset.col) === columnIndex) {
                th.classList.add(sortOrder === 'asc' ? 'sort-asc' : 'sort-desc');
            }
        });
    }

    sortableHeaders().forEach(th => {
        th.addEventListener('click', () => sortTable(Number(th.dataset.col)));
    });

    // ============================== downloads ==============================

    function formatCell(cellContent) {
        const escaped = String(cellContent).replace(/"/g, '""');
        return /[",\n]/.test(escaped) ? `"${escaped}"` : escaped;
    }

    function generateCSV(data, queryGene) {
        if (!data || !data.nodes || !data.edges) {
            alert('Data is incomplete.');
            return '';
        }

        const csv = [];
        csv.push([
            'Gene Name', 'Oncogene', 'Gene ecDNA Count', 'Intersection Count',
            'Coamplification Frequency', 'Location', 'Distance (bp)',
            'P-Value Single Interval Test', 'Q-value Single Interval Test', 'Odds Ratio Single Interval Test',
            'P-Value Multi Interval Test', 'Q-value Multi Interval Test', 'Odds Ratio Multi Interval Test',
            'P-Value Multi Chromosomal Test', 'Q-value Multi Chromosomal Test', 'Odds Ratio Multi Chromosomal Test',
            'Gene ecDNA Samples', 'Intersection Samples'
        ].join(','));

        const edgeMap = new Map();
        data.edges.forEach(edge => {
            edgeMap.set(`${edge.data.source}|${edge.data.target}`, edge.data);
            edgeMap.set(`${edge.data.target}|${edge.data.source}`, edge.data);
        });

        const rows = data.nodes.map(node => {
            const n = node.data;
            const e = edgeMap.get(`${queryGene}|${n.label}`) || {};
            return {
                gene: n.label,
                oncogene: n.oncogene || 'False',
                sample_count: n.samples ? n.samples.length : 'N/A',
                inter_count: e.inter ? e.inter.length : 'N/A',
                weight: e.weight != null ? e.weight : -1,
                location: n.location ? `${n.location[0]}:${n.location[1]}-${n.location[2]}` : 'N/A',
                distance: e.distance != null ? e.distance : 'N/A',
                pval_single_interval: e.pval_single_interval,
                qval_single_interval: e.qval_single_interval,
                odds_ratio_single_interval: e.odds_ratio_single_interval,
                pval_multi_interval: e.pval_multi_interval,
                qval_multi_interval: e.qval_multi_interval,
                odds_ratio_multi_interval: e.odds_ratio_multi_interval,
                pval_multi_chromosomal: e.pval_multi_chromosomal,
                qval_multi_chromosomal: e.qval_multi_chromosomal,
                odds_ratio_multi_chromosomal: e.odds_ratio_multi_chromosomal,
                gene_samples: n.samples ? `["${n.samples.join('", "')}"]` : 'N/A',
                inter: e.inter ? `["${e.inter.join('", "')}"]` : 'N/A'
            };
        });

        const queryRow = rows.find(r => r.gene === queryGene);
        const otherRows = rows.filter(r => r.gene !== queryGene);
        otherRows.sort((a, b) => (b.weight != null ? b.weight : -1) - (a.weight != null ? a.weight : -1));

        [queryRow, ...otherRows].filter(Boolean).forEach(row => {
            csv.push([
                row.gene,
                row.oncogene,
                row.sample_count,
                row.inter_count,
                stat(row.weight),
                row.location,
                row.distance === -1 ? 'N/A' : row.distance,
                stat(row.pval_single_interval),
                stat(row.qval_single_interval),
                stat(row.odds_ratio_single_interval),
                stat(row.pval_multi_interval),
                stat(row.qval_multi_interval),
                stat(row.odds_ratio_multi_interval),
                stat(row.pval_multi_chromosomal),
                stat(row.qval_multi_chromosomal),
                stat(row.odds_ratio_multi_chromosomal),
                row.gene_samples,
                row.inter
            ].map(formatCell).join(','));
        });

        return csv.join('\n');
    }

    function timestamp() {
        return new Date().toISOString().replace(/:/g, '-').replace('T', '_').split('.')[0];
    }

    function triggerDownload(blob, filename) {
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        setTimeout(() => {
            document.body.removeChild(link);
            URL.revokeObjectURL(link.href);
        }, 100);
    }

    el.downloadCurrent.addEventListener('click', function () {
        if (!completeData || !inputNode || !cy) {
            alert('No graph data available. Please search for a gene first.');
            return;
        }

        // Export exactly what is drawn, so the file matches the button label and
        // the top-N limit. "All data" covers the unfiltered case.
        const shown = {
            nodes: cy.nodes().map(n => ({ data: n.data() })),
            edges: cy.edges().map(e => ({ data: e.data() }))
        };

        const csvContent = generateCSV(shown, inputNode);
        if (!csvContent) { return; }

        triggerDownload(new Blob([csvContent], { type: 'text/csv' }),
                        `AACoampGraph_${inputNode}_${timestamp()}.csv`);
    });

    el.downloadSvg.addEventListener('click', function (e) {
        e.stopPropagation();
        if (!cy) {
            alert('No graph is currently displayed.');
            return;
        }
        try {
            const svgContent = cy.svg({ full: true, scale: 2, bg: '#ffffff' });
            triggerDownload(new Blob([svgContent], { type: 'image/svg+xml;charset=utf-8' }),
                            `AACoampGraph_${inputNode}_${timestamp()}.svg`);
        } catch (error) {
            console.error('Error in SVG download:', error);
            alert('Error creating SVG: ' + error.message);
        }
    });

    const downloadAllLabel = el.downloadAll.textContent;

    el.downloadAll.addEventListener('click', async function (e) {
        e.stopPropagation();
        try {
            el.downloadAll.disabled = true;
            el.downloadAll.textContent = 'Downloading…';

            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 300000); // 5 minutes

            const response = await fetch('/coamplification-graph/download-edges/?include_samples=true',
                                         { signal: controller.signal });
            clearTimeout(timeoutId);

            if (!response.ok) {
                throw new Error(`Server error: ${response.status}`);
            }

            triggerDownload(await response.blob(), `AACoampGraph_full_${timestamp()}.csv`);
        } catch (error) {
            if (error.name === 'AbortError') {
                alert('Download timed out. The file may be too large. Please try again or contact support.');
            } else {
                console.error('Error downloading full CSV:', error);
                alert('Error downloading CSV: ' + error.message);
            }
        } finally {
            el.downloadAll.disabled = false;
            el.downloadAll.textContent = downloadAllLabel;
        }
    });

    // ================================ init =================================

    refreshFilterAffordances();
    setDataTabsEnabled(false);
    el.downloadCurrent.disabled = true;
    el.downloadSvg.disabled = true;
    updateLimitBounds();
    fetchOverview();
    el.search.focus();
});
