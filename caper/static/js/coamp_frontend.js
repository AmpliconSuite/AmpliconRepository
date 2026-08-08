window.addEventListener('DOMContentLoaded', function () {
    // Global variables
    let cy = null
    let nodeID = {};
    let allTooltips = {};
    let inputNode = null
    let total_data = 0;
    let completeData = null;
    // column index of "Coamplification Frequency" in #data-table
    const COL_COAMP_FREQ = 4;
    console.log(document.styleSheets)

    cytoscape.use( cytoscapePopper(tippyFactory) );

    try {
        cytoscape.use(cytoscapeSvg);
        console.log("Cytoscape SVG extension loaded");
    } catch (e) {
        console.error("Failed to load cytoscape-svg:", e);
    }

    const projectStats = JSON.parse(document.getElementById('project-stats-data').textContent);

    const sampleList = document.getElementById('sampleList');
    sampleList.innerHTML = '';

    Object.entries(projectStats).forEach(([project, [sampleCount, ecDNACount]]) => {
        const li = document.createElement('li');
        li.textContent = `${project} — ${sampleCount} sample${sampleCount !== 1 ? 's' : ''}, ${ecDNACount} ecDNA features`;
        sampleList.appendChild(li);
    });

    // ----------------------------- Neo4j interaction -----------------------------
    async function fetchSubgraph() {
        console.log("Load graph pressed");

        // input gene
        const requestedNode = $('#textBox').val().trim().toUpperCase();

        // alert
        if (!requestedNode) {
            alert("Please enter a gene name.");
            return;
        }

        // These filters are applied by the query itself, so changing one needs a
        // round trip. The gene-count slider is not among them - see renderGraph().
        const minWeight = parseFloat($('#edgeWeight').val());
        const sampleMinimum = parseFloat($('#numSamples').val());
        const oncogenesChecked = $('#oncogenes_only').is(':checked');
        const allEdgesChecked = false;
        // const allEdgesChecked = $('#all_edges').is(':checked');

        // Fetch the subgraph data from Flask server
        try {
            // const response = await fetch(`http://127.0.0.1:5000/getNodeData?name=${inputNode}&min_weight=${minWeight}&min_samples=${sampleMinimum}&oncogenes=${oncogenesChecked}&all_edges=${allEdgesChecked}`);
            const response = await fetch(`/coamplification-graph/visualizer/${requestedNode}/?min_weight=${minWeight}&min_samples=${sampleMinimum}&oncogenes=${oncogenesChecked}&all_edges=${allEdgesChecked}`);
            if (!response.ok) {
                throw new Error(`Node ${requestedNode} not found or server error.`);
            }

            const data = await response.json();

            // Store the complete data for later use in CSV export
            completeData = data;
            inputNode = requestedNode;
            total_data = data.nodes.length;

            markFiltersFresh();
            renderGraph();
        } catch (error) {
            alert(error.message);
        }
    }

    // Draw the Cytoscape view from data already fetched. The gene-count slider only
    // trims what is shown, so it re-renders through here instead of re-querying.
    function renderGraph() {
        if (!completeData || !inputNode) { return; }

        removeAllTooltips();

        cy = null
        nodeID = {};

        // Clear any existing graph
        document.getElementById('cy').innerHTML = '';

        const limit = parseInt($('#limit').val());
        const filtered_data = filterData(completeData, limit)

        try {
            // Initialize Cytoscape with fetched data
            cy = cytoscape({
                container: document.getElementById('cy'),
                elements: filtered_data,  // Use the data from the server
                style: [
                    {
                        selector: 'node',
                        style: {
                            'background-color': '#A7C6ED',
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
                        selector: `node[label="${inputNode}"]`,
                        style: {
                            'z-index': 100,
                            'background-color': '#ff4757',
                            'color': '#fff', // Changed back to white
                            'font-weight': 'bold',
                            'font-size': '14px',
                            'text-outline-width': 2,
                            'text-outline-color': '#ff4757'
                        }
                    },
                    {
                        selector: `node[oncogene="True"]`,
                        style: {
                            'background-color': '#ff4757',
                            'z-index': 10,
                            'color': '#fff', // Changed back to white
                            'font-weight': 'bold',
                            'text-outline-width': 2,
                            'text-outline-color': '#ff4757'
                        }
                    },
                    { selector: 'edge', style: { 'width': 1, 'line-color': 'gray' } },  // Default for edges
                    { selector: 'edge.significant', style: { 'width': 3, 'line-color': 'orange' } }, // Highlight significant edges
                    { selector: '.highlighted', style: {'z-index': 100, 'background-color': '#ffd500', 'line-color': '#ffd500' } }
                ]
            });


            // Dictionary to access node ids by name
            cy.nodes().forEach(node => {
                nodeID[node.data('label')] = "#"+node.id();
            });
            console.log('Number of nodes:', Object.keys(nodeID).length);
            console.log(nodeID[inputNode] + ': ' + cy.$(nodeID[inputNode]).data('label'));

            // Update sample slider max
            updateSampleMax(cy);
            // Updata limit slider
            updateLimitMax(cy);
            //styleNodes(cy, inputNode);
            layout(cy, inputNode);
            // Make tooltips for all elements
            makeTips(cy);

            // Ensure the graph is fully visible and expanded
            cy.ready(() => {
                cy.fit();  // Adjusts the viewport to fit all elements
                cy.zoom(1); // Optionally set zoom level (1 = default)
                cy.resize();
            });

            // Remove existing SVG button if it exists
            const existingSvgBtn = document.getElementById('download-svg-btn');
            if (existingSvgBtn) {
                existingSvgBtn.remove();
            }

            // Create SVG download button
            const buttonContainer = document.querySelector('.filter-right');
            const downloadSvgBtn = document.createElement('button');
            downloadSvgBtn.id = 'download-svg-btn';
            downloadSvgBtn.innerHTML = 'Download SVG';

            // Add the button to the button container
            buttonContainer.appendChild(downloadSvgBtn);

            // Add click handler for SVG download (with proper access to cy and inputNode)
            downloadSvgBtn.addEventListener('click', function(e) {
                console.log("SVG download button clicked");
                e.stopPropagation(); // Prevent event from bubbling to cy container

                if (!cy) {
                    console.error("Error: Cytoscape instance not available");
                    alert('No graph is currently displayed.');
                    return;
                }

                try {
                    // Create a new blob with the SVG content
                    const svgContent = cy.svg({
                        full: true,  // Export the full rendered image
                        scale: 2,    // Higher quality export
                        bg: '#ffffff'  // White background
                    });

                    console.log("SVG content created");

                    // Create a Blob with the SVG content
                    const blob = new Blob([svgContent], {
                        type: 'image/svg+xml;charset=utf-8'
                    });

                    // Create a download link
                    const link = document.createElement('a');
                    link.href = URL.createObjectURL(blob);

                    // Generate filename with timestamp
                    const now = new Date();
                    const formattedDate = now.toISOString().replace(/:/g, '-').replace('T', '_').split('.')[0];
                    const filename = `AACoampGraph_${inputNode}_${formattedDate}.svg`;
                    link.download = filename;

                    console.log("Triggering download: " + filename);

                    // Trigger download
                    document.body.appendChild(link);
                    link.click();

                    // Cleanup
                    document.body.removeChild(link);
                    URL.revokeObjectURL(link.href);

                    console.log("Download completed");
                } catch (error) {
                    console.error("Error in SVG download:", error);
                    alert("Error creating SVG: " + error.message);
                }
            });

            // Initialize Gene Data Column with fetched data
            const datacontainer = document.getElementById('data-container');
            datacontainer.innerHTML = ''; // Clear previous rows

            // let rownumber = 0;

            cy.nodes().forEach(node => {
                const row = document.createElement('tr');

                // rownumber_element = document.createElement('td');
                // rownumber_element.textContent = rownumber;

                const cellName = document.createElement('td');
                const geneName = node.data('label');
                const link = document.createElement('a');

                // Set the href attribute to the desired URL (customize this URL as needed)
                link.href = `https://depmap.org/portal/gene/${geneName}?tab=overview`;
                link.textContent = geneName; // Set the text to the gene name
                link.target = '_blank'; // Open the link in a new tab (optional)

                cellName.appendChild(link);

                cellStatus = document.createElement('td');
                cellStatus.textContent = node.data('oncogene');

                edges = node.edgesWith(cy.$(nodeID[inputNode]));

                // how many samples amplify this gene at all
                const cellAmps = document.createElement('td');
                const samples = node.data('samples');
                cellAmps.textContent = samples ? String(samples.length) : 'N/A';

                // how many of those also amplify the query gene. The query gene has no
                // edge to itself, so it gets a dash rather than a misleading zero.
                const cellCoamps = document.createElement('td');
                cellCoamps.textContent = geneName === inputNode
                    ? '—'
                    : String(edges[0]?.data('leninter') ?? 'N/A');

                cellWeight = document.createElement('td');
                cellWeight.textContent = String(edges[0]?.data('weight').toFixed(3) ?? 'N/A');

                // row.appendChild(rownumber_element);
                row.appendChild(cellName);
                row.appendChild(cellStatus);
                row.appendChild(cellAmps);
                row.appendChild(cellCoamps);
                row.appendChild(cellWeight);


                datacontainer.appendChild(row);

                // rownumber++;

                // Add click event to each row
                row.addEventListener('click', (event) => {
                    const nodeName = cellName.textContent; // Assuming cellName text contains node ID
                    const node = cy.$(nodeID[nodeName]);
                    node.emit('tap');

                    const clickedRow = event.currentTarget;
                    clickedRow.classList.toggle('active');
                    console.log(clickedRow);
                });
            });
            // initialize sorted by co-amp frequency (last column)
            sortTable(COL_COAMP_FREQ, 'desc');

            // Resize elements on tap
            cy.on('tap', 'edge', (event) => {
                const edge = event.target;
                const width = Number(edge.style('width').replace('px',''));
                const scale = 3;
                const newWidth = edge.hasClass('highlighted') ? width*scale : width/scale;
                edge.animate({
                    style: { 'width': newWidth } // Increase edge width
                    }, {
                    duration: 300,       // Duration in ms
                    easing: 'ease-in-out'
                });
            });
            cy.on('tap', 'node', (event) => {
                const node = event.target;
                const size = node.data('size');
                const scale = 1.3;
                const newSize = node.hasClass('highlighted') ? size*scale : size/scale;
                node.animate({
                    style: { 'width': newSize, 'height': newSize } // Increase size
                    }, {
                    duration: 300,       // Duration in ms
                    easing: 'ease-in-out'
                });
            });

            // A fresh Cytoscape instance starts with no edge classes, so re-apply the
            // significance highlighting the sliders are currently asking for.
            applySignificance();

        } catch (error) {
            alert(error.message);
        }

    }

    // ----------------------------- Tooltip functions -----------------------------
    function tippyFactory(ref, content, theme) {
        // tippy constructor requires DOM element/elements so create a placeholder
        var dummyDomEle = document.createElement('div');

        var tip = tippy( dummyDomEle, {
            getReferenceClientRect: ref.getBoundingClientRect,
            trigger: 'manual', // mandatory
            // dom element inside the tippy:
            content: content,
            // preferences:
            arrow: false,
            placement: 'bottom-end',
            hideOnClick: false,
            sticky: "reference",
            theme: theme,
            allowHTML: true,

            // if interactive:
            interactive: true,
            appendTo: document.body
        } );

        return tip;
    }

    // Set tooltip content
    function createTooltipContent(ele) {
        let content = '';
        if (ele.isNode()) {
            let template = document.getElementById('node-template');
            template.querySelector('#ntip-name').textContent = ele.data('label') || 'N/A';
            template.querySelector('#ntip-location').textContent =
            ele.data('location')
                ? `${ele.data('location')[0]}:${ele.data('location')[1]}-${ele.data('location')[2]}`
                : 'N/A';
            template.querySelector('#ntip-oncogene').textContent = ele.data('oncogene') || 'N/A';
            template.querySelector('#ntip-nsamples').textContent = ele.data('samples').length || 'N/A';
            template.querySelector('#ntip-samples').textContent = ele.data('samples').join(', ') || 'N/A';
            content = template.innerHTML;
        }
        else {
            let template = document.getElementById('edge-template');
            
            template.querySelector('#etip-name').textContent = ele.data('label') || 'N/A';
            template.querySelector('#etip-weight').textContent = ele.data('weight').toFixed(3) || 'N/A';
            template.querySelector('#etip-frac').textContent = ele.data('leninter') + '/' + ele.data('lenunion');
            template.querySelector('#etip-distance').textContent = ele.data('distance') < 0 ? 'N/A' : ele.data('distance');
            template.querySelector('#etip-pval-single_interval').textContent = ele.data('pval_single_interval') < 0 ? 'N/A' : ele.data('pval_single_interval').toFixed(3);
            template.querySelector('#etip-qval-single_interval').textContent = ele.data('qval_single_interval') < 0 ? 'N/A' : ele.data('qval_single_interval').toFixed(3);
            template.querySelector('#etip-odds_ratio-single_interval').textContent = ele.data('odds_ratio_single_interval') < 0 ? 'N/A' : ele.data('odds_ratio_single_interval').toFixed(3);
            template.querySelector('#etip-pval-multi_interval').textContent = ele.data('pval_multi_interval') < 0 ? 'N/A' : ele.data('pval_multi_interval').toFixed(3);
            template.querySelector('#etip-qval-multi_interval').textContent = ele.data('qval_multi_interval') < 0 ? 'N/A' : ele.data('qval_multi_interval').toFixed(3);
            template.querySelector('#etip-odds_ratio-multi_interval').textContent = ele.data('odds_ratio_multi_interval') < 0 ? 'N/A' : ele.data('odds_ratio_multi_interval').toFixed(3);
            template.querySelector('#etip-pval-multi_chromosomal').textContent = ele.data('pval_multi_chromosomal') < 0 ? 'N/A' : ele.data('pval_multi_chromosomal').toFixed(3);
            template.querySelector('#etip-qval-multi_chromosomal').textContent = ele.data('qval_multi_chromosomal') < 0 ? 'N/A' : ele.data('qval_multi_chromosomal').toFixed(3);
            template.querySelector('#etip-odds_ratio-multi_chromosomal').textContent = ele.data('odds_ratio_multi_chromosomal') < 0 ? 'N/A' : ele.data('odds_ratio_multi_chromosomal').toFixed(3);
            template.querySelector('#etip-nsamples').textContent = ele.data('leninter') || 'N/A';
            template.querySelector('#etip-samples').textContent = ele.data('inter').join(', ') || 'N/A';
            content = template.innerHTML;
        }
        return content;
    }

    function makeTips(cy) {
        if (!cy) { return }
        // Dict to store tooltips in case later reference needed
        const tooltips = {};
        cy.ready(() => {
            cy.elements().forEach((ele) => {
                // Get the type (node or edge)
                const theme = ele.isNode() ? 'node' : 'edge';
                // Get the properties to show in the tooltip content
                const content = createTooltipContent(ele);

                const popperRef = ele.popperRef();
                // Create tooltip
                const tooltip = tippyFactory(popperRef, content, theme);
                
                // Add event listener for close button after tooltip is shown
                tooltip.popper.addEventListener('click', (event) => {
                    if (event.target.classList.contains('tooltip-close-btn')) {
                        ele.removeClass('highlighted');
                        tooltip.hide();
                        event.stopPropagation();
                    }
                });
                
                // Show/hide tooltip on click
                ele.on('tap', () => {
                    ele.toggleClass('highlighted');
                    tooltip.state.isVisible ? tooltip.hide() : tooltip.show();
                });
                allTooltips[ele.id()] = tooltip;
                tooltips[ele.id()] = tooltip;
            });
        });
    }

    function removeAllTooltips() {
        Object.values(allTooltips).forEach(tooltip => {
            tooltip.hide(); // Hide the tooltip
            tooltip.destroy(); // Destroy the tooltip instance
        });
        allTooltips = {}; // Reset the object
    }

    // ----------------------------- Cytoscape layout ------------------------------
    function layout(cy, input) {
        if (!cy) { return }
        const radius = 40;

        // Set node sizes based on their relationship to the input node
        cy.nodes().forEach(node => {
            if (node.data('label') === input) {
                const size = radius * 1.5;
                node.style({ 'width': size, 'height': size });
                node.data('size', size);
            }
            else {
                const edges = node.edgesWith(cy.$(nodeID[input]));
                const scale = edges.reduce((sum, edge) => sum + edge.data('weight'), 0);
                const size = radius * (0.8 + scale);
                node.style({ 'width': size, 'height': size });
                node.data('size', size);
            }
        });

        // Improved layout settings
        cy.layout({
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
            nodeRepulsion: (node) => {
                return 4500 - node.data('size') * 50;
            }
        }).run();

        // Ensure the graph is properly centered and sized
        setTimeout(() => {
            cy.fit();
            cy.center();
        }, 500);
    }

    // ------------------------------ Filter elements -------------------------------

    // update graph with buttons
    // $('#storeButton').on('click', function() {
    //     document.getElementById('storedText').textContent = "PRESSED";
    // });
    $('#storeButton').on('click', fetchSubgraph);
    $('#textBox').on('keydown', function(event) {
    if (event.key === "Enter") {
        event.preventDefault(); // prevent default newline behavior
        $('#storeButton').click(); // simulate button click
        }
    });
    $('#filterButton').on('click', fetchSubgraph);

    // Re-colour the edges for the current significance test and threshold. Bound
    // once here rather than per graph load, which used to stack up a duplicate
    // listener on #sigThreshold every time a graph was drawn.
    function applySignificance() {
        const threshold = parseFloat(document.getElementById('sigThreshold').value);
        const testRadios = document.getElementsByName("sigTest");
        let selectedTest = "any"; // default fallback

        for (const radio of testRadios) {
            if (radio.checked) {
                selectedTest = radio.value;
                break;
            }
        }

        document.getElementById('qValue').textContent = threshold;

        if (cy) {
            cy.edges().forEach(edge => {
                let isSignificant = false;

                if (selectedTest === "any") {
                    // Check all q-values
                    const qvals = [
                        parseFloat(edge.data('qval_single_interval')),
                        parseFloat(edge.data('qval_multi_interval')),
                        parseFloat(edge.data('qval_multi_chromosomal'))
                    ];

                    for (const q of qvals) {
                        if (!isNaN(q) && q <= threshold && q >= 0) {
                            isSignificant = true;
                            break;
                        }
                    }
                } else {
                    const qvalKey = `qval_${selectedTest}`;
                    const qval = parseFloat(edge.data(qvalKey));
                    if (!isNaN(qval) && qval <= threshold && qval >= 0) {
                        isSignificant = true;
                    }
                }

                if (isSignificant) {
                    edge.addClass('significant');
                } else {
                    edge.removeClass('significant');
                }
            });
        }
    }

    document.getElementById('sigThreshold').addEventListener('input', applySignificance);

    // Update graph based on chosen signficance test
    document.querySelectorAll('input[name="sigTest"]').forEach(radio => {
        radio.addEventListener('change', applySignificance);
    });

    // ---- "needs re-submitting" indicator ----
    // The filters below are applied by the Neo4j query, so the displayed graph is
    // out of date until Filter is clicked again. Flag that instead of silently
    // leaving a stale graph on screen.
    const filterButton = document.getElementById('filterButton');
    const filterStale = document.getElementById('filterStale');

    function markFiltersStale() {
        if (!completeData) { return; }   // nothing drawn yet, so nothing is stale
        filterButton.classList.add('stale');
        filterStale.hidden = false;
    }

    function markFiltersFresh() {
        filterButton.classList.remove('stale');
        filterStale.hidden = true;
    }

    ['edgeWeight', 'numSamples', 'oncogenes_only'].forEach(id => {
        document.getElementById(id).addEventListener('change', markFiltersStale);
    });

    document.getElementById('edgeWeight').addEventListener('input', function() {
        document.getElementById('sliderValue').textContent = this.value;
    });
    document.getElementById('numSamples').addEventListener('input', function() {
        document.getElementById('sampleValue').textContent = this.value;
    });
    document.getElementById('limit').addEventListener('input', function () {
        document.getElementById('sliderTooltip').textContent = this.value;
    });
    // The gene-count slider only trims data we already hold, so redraw on release
    // (not on every 'input' tick, which would relayout the graph mid-drag).
    document.getElementById('limit').addEventListener('change', function () {
        renderGraph();
    });
    // update max values
    function updateSampleMax(cy) {
        if (cy) {
            maxSamples = 1;
            cy.edges().forEach(edge => {
                const samples = edge.data('lenunion');
                if (samples > maxSamples) {
                    maxSamples = samples;
                }
            });
            document.getElementById('numSamples').max = maxSamples;
            document.getElementById('sampleMaxText').textContent = maxSamples;
        }
    }

    function updateLimitMax(cy) {
        if (cy) {
            document.getElementById('queryResult').textContent = total_data-1;
            document.getElementById('limit').max = total_data-1;
            document.getElementById('limitMaxText').textContent = total_data-1;
            document.getElementById('limitMinText').textContent = 1;
            document.getElementById('sliderTooltip').textContent = cy.nodes().length-1;
        }
    }

    function filterData(data, topN) {
        // Sort edges by weight in descending order
        const sortedEdges = data.edges.sort((a, b) => b.data.weight - a.data.weight);

        // Select the top N edges
        const topEdges = sortedEdges.slice(0, topN);

        // Get the set of node IDs referenced in the top edges
        const nodeIds = new Set();
        topEdges.forEach(edge => {
        nodeIds.add(edge.data.source);
        nodeIds.add(edge.data.target);
        });

        // Filter nodes to include only those in the nodeIds set
        const filteredNodes = data.nodes.filter(node => nodeIds.has(node.data.id));

        // Return the filtered dataset
        return {
        edges: topEdges,
        nodes: filteredNodes
        };
    }

    // ---------------------------- Table and Download -----------------------------
    // Function to sort the table when clicking on column headers
    function sortTable(columnIndex, defaultOrder = null) {
        const table = document.getElementById('data-table');
        const tbody = document.getElementById('data-container');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const noDataRow = tbody.querySelector('.no-data');
        // Remove the "No data available" row temporarily if present
        if (noDataRow) rows.splice(rows.indexOf(noDataRow), 1);
        // Toggle sort order
        let sortOrder;
        if (defaultOrder) {
            sortOrder = defaultOrder;
        } else {
            sortOrder = table.dataset.sortOrder === 'asc' ? 'desc' : 'asc';
        }
        table.dataset.sortOrder = sortOrder;

        // The query gene has no edge to itself, so its numeric cells read 'N/A'/'—'.
        // Pin it to the top instead of letting it sort as text among the numbers -
        // that made the comparator inconsistent and left the rest in arbitrary order.
        // generateCSV() puts the query gene first for the same reason.
        const queryRows = rows.filter(r => r.children[0].innerText.trim() === inputNode);
        const sortable = rows.filter(r => r.children[0].innerText.trim() !== inputNode);

        // Sort rows based on the content of the selected column
        sortable.sort((a, b) => {
            const cellA = a.children[columnIndex].innerText.trim();
            const cellB = b.children[columnIndex].innerText.trim();
            const numA = parseFloat(cellA);
            const numB = parseFloat(cellB);
            const aIsNum = cellA !== '' && !isNaN(numA);
            const bIsNum = cellB !== '' && !isNaN(numB);

            if (aIsNum && bIsNum) {
                // Numeric sort
                return sortOrder === 'asc' ? numA - numB : numB - numA;
            }
            if (aIsNum !== bIsNum) {
                // Keep values without a number after the ones that have one, in both
                // directions, so the comparator stays transitive.
                return aIsNum ? -1 : 1;
            }
            // Text sort
            return sortOrder === 'asc'
                ? cellA.localeCompare(cellB)
                : cellB.localeCompare(cellA);
        });

        rows.length = 0;
        rows.push(...queryRows, ...sortable);

        // let rownumber = 1;
        // // Re-add sorted rows to the tbody
        // rows.forEach(
        //     row => {
        //         row.children[0].innerText = rownumber;
        //         tbody.appendChild(row);
        //         rownumber++;
        //     }
        // )
        rows.forEach(row => tbody.appendChild(row));

        // Re-add the "No data available" row if needed
        if (noDataRow && rows.length === 0) tbody.appendChild(noDataRow);

        updateSortIndicators(columnIndex, sortOrder);
    }

    // Add sort indicators (▲▼)
    function updateSortIndicators(columnIndex, sortOrder) {
        document.querySelectorAll('#data-table th').forEach((th, i) => {
            th.classList.remove('sort-asc', 'sort-desc');
            if (i === columnIndex) {
            th.classList.add(sortOrder === 'asc' ? 'sort-asc' : 'sort-desc');
            }
        });
    }

    // Attach click listeners for sorting
    document.querySelectorAll('#data-table th').forEach((header, index) => {
        header.style.cursor = 'pointer';
        header.addEventListener('click', () => sortTable(index));
    });

    // Helper function to format lists for csv, accounting for quotes
    function formatCell(cellContent) {
        const str = String(cellContent);

        // Escape quotes by doubling them
        const escaped = str.replace(/"/g, '""');

        // If the field contains a comma, quote, or newline, wrap in quotes
        if (/[",\n]/.test(escaped)) {
            return `"${escaped}"`;
        }

        return escaped;
    }

    // Function to generate CSV
    function generateCSV(data, inputNode) {
        if (!data || !data.nodes || !data.edges) {
            alert('Data is incomplete.');
            return '';
        }

        const csv = [];
        const header = ['Gene Name', 'Oncogene', 'Gene ecDNA Count', 'Intersection Count', 'Coamplification Frequency', 'Location', 'Distance (bp)', 'P-Value Single Interval Test', 'Q-value Single Interval Test', 'Odds Ratio Single Interval Test', 'P-Value Multi Interval Test', 'Q-value Multi Interval Test', 'Odds Ratio Multi Interval Test', 'P-Value Multi Chromosomal Test', 'Q-value Multi Chromosomal Test', 'Odds Ratio Multi Chromosomal Test','Gene ecDNA Samples', 'Intersection Samples'];
        csv.push(header.join(','));

        const nodes = data.nodes;
        const edges = data.edges;

        // Map edges for fast lookup
        const edgeMap = new Map();
        edges.forEach(edge => {
            const key1 = `${edge.data.source}|${edge.data.target}`;
            const key2 = `${edge.data.target}|${edge.data.source}`;
            edgeMap.set(key1, edge.data);
            edgeMap.set(key2, edge.data);
        });

        // Build node + edge data rows
        const rows = nodes.map(node => {
            const nData = node.data;
            const key = `${inputNode}|${nData.label}`;
            const edgeData = edgeMap.get(key) || {};

            return {
                gene: nData.label,
                oncogene: nData.oncogene || 'False',
                sample_count: nData.samples ? nData.samples.length : 'N/A',
                inter_count: edgeData.inter ? edgeData.inter.length : 'N/A',
                weight: edgeData.weight ?? -1,
                location: nData.location ? `${nData.location[0]}:${nData.location[1]}-${nData.location[2]}` : 'N/A',
                distance: edgeData.distance ?? 'N/A',
                pval_single_interval: edgeData.pval_single_interval ?? 'N/A',
                qval_single_interval: edgeData.qval_single_interval ?? 'N/A',
                odds_ratio_single_interval: edgeData.odds_ratio_single_interval ?? 'N/A',
                pval_multi_interval: edgeData.pval_multi_interval ?? 'N/A',
                qval_multi_interval: edgeData.qval_multi_interval ?? 'N/A',
                odds_ratio_multi_interval: edgeData.odds_ratio_multi_interval ?? 'N/A',
                pval_multi_chromosomal: edgeData.pval_multi_chromosomal ?? 'N/A',
                qval_multi_chromosomal: edgeData.qval_multi_chromosomal ?? 'N/A',
                odds_ratio_multi_chromosomal: edgeData.odds_ratio_multi_chromosomal ?? 'N/A',
                gene_samples: nData.samples ? `["${nData.samples.join('", "')}"]` : 'N/A',
                inter: edgeData.inter ? `["${edgeData.inter.join('", "')}"]` : 'N/A',
            };
        });

        // Move inputNode to top
        const queryRow = rows.find(r => r.gene === inputNode);
        const otherRows = rows.filter(r => r.gene !== inputNode);

        // Sort other rows by coamplification frequency descending
        otherRows.sort((a, b) => (b.weight ?? -1) - (a.weight ?? -1));

        // -1 is the sentinel the graph uses for "not computed", and the query gene's
        // own row has no edge at all, so it carries 'N/A'. Both must survive
        // formatting - parseFloat('N/A').toFixed(3) used to emit a literal "NaN".
        const stat = (v) => {
            const n = parseFloat(v);
            return (v === -1 || isNaN(n)) ? 'N/A' : n.toFixed(3);
        };

        // Combine and format
        const finalRows = [queryRow, ...otherRows];
        finalRows.forEach(row => {
            const formattedRow = [
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
                row.inter,
            ].map(formatCell);

            csv.push(formattedRow.join(','));
        });

        return csv.join('\n');
    }

    // Add event listener for the download button
    // First, remove any existing event listeners to prevent multiple downloads
    const downloadBtn = document.getElementById('download-btn');
    const newDownloadBtn = downloadBtn.cloneNode(true);
    downloadBtn.parentNode.replaceChild(newDownloadBtn, downloadBtn);

    // Single event listener for Download CSV button
    newDownloadBtn.addEventListener('click', function() {
        // Make sure completeData and inputNode are available
        if (!completeData || !inputNode) {
            alert('No graph data available. Please load a graph first.');
            return;
        }

        // Export exactly what is on screen, so the file matches the button label and
        // the gene-count slider. "Download all data" covers the unfiltered case.
        const shownData = cy
            ? { nodes: cy.nodes().map(n => ({ data: n.data() })),
                edges: cy.edges().map(e => ({ data: e.data() })) }
            : completeData;

        const csvContent = generateCSV(shownData, inputNode);

        if (!csvContent) {
            return; // Exit if CSV generation failed
        }

        const blob = new Blob([csvContent], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);

        const link = document.createElement('a');
        link.href = url;
        const now = new Date();
        const formattedDate = now.toISOString().replace(/:/g, '-').replace('T', '_').split('.')[0];
        link.download = `AACoampGraph_${inputNode}_${formattedDate}.csv`;

        document.body.appendChild(link);
        link.click();

        setTimeout(() => {
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
        }, 100);
    });

    // "Download all data" - the whole co-amplification graph, server-side. Bound once
    // here; it does not depend on the displayed graph.
    const downloadAllBtn = document.getElementById('download-full-csv-btn');
    const downloadAllLabel = downloadAllBtn.innerHTML;

    downloadAllBtn.addEventListener('click', async function(e) {
        console.log("Full CSV download button clicked");
        e.stopPropagation();

        try {
            // Show loading indicator
            downloadAllBtn.disabled = true;
            downloadAllBtn.innerHTML = 'Downloading...';

            // Create AbortController with long timeout (5 minutes)
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 300000); // 5 minutes

            // Fetch the CSV with include_samples=true
            const response = await fetch('/coamplification-graph/download-edges/?include_samples=true', {
                signal: controller.signal
            });

            clearTimeout(timeoutId);

            if (!response.ok) {
                throw new Error(`Server error: ${response.status}`);
            }

            // Get the blob from response
            const blob = await response.blob();

            // Create download link
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);

            // Generate filename with timestamp
            const now = new Date();
            const formattedDate = now.toISOString().replace(/:/g, '-').replace('T', '_').split('.')[0];
            link.download = `AACoampGraph_full_${formattedDate}.csv`;

            console.log("Triggering download: " + link.download);

            // Trigger download
            document.body.appendChild(link);
            link.click();

            // Cleanup
            document.body.removeChild(link);
            URL.revokeObjectURL(link.href);

            console.log("Download completed");
        } catch (error) {
            if (error.name === 'AbortError') {
                console.error("Download timed out after 5 minutes");
                alert("Download timed out. The file may be too large. Please try again or contact support.");
            } else {
                console.error("Error downloading full CSV:", error);
                alert("Error downloading CSV: " + error.message);
            }
        } finally {
            // Reset button state
            downloadAllBtn.disabled = false;
            downloadAllBtn.innerHTML = downloadAllLabel;
        }
    });
});