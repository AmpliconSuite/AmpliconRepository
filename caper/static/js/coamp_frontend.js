window.addEventListener('DOMContentLoaded', function () {
    // Global variables
    let cy = null
    let nodeID = {};
    let allTooltips = {};
    let inputNode = null
    let total_data = 0;
    let completeData = null;
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

    // Update gene name in dropdown when a gene is loaded
    function updateGeneNameDisplay(geneName) {
        const geneNameDisplay = document.getElementById('gene-name-display');
        if (geneNameDisplay) {
            geneNameDisplay.textContent = geneName || 'gene';
        }
    }

    // ----------------------------- Neo4j interaction -----------------------------
    async function fetchSubgraph() {
        console.log("Load graph pressed");

        removeAllTooltips();

        cy = null
        nodeID = {};
        inputNode = null

        // input gene
        inputNode = $('#textBox').val().trim().toUpperCase();

        // filters
        const minWeight = parseFloat($('#edgeWeight').val());
        const sampleMinimum = parseFloat($('#numSamples').val());
        const oncogenesChecked = $('#oncogenes_only').is(':checked');
        const limit = parseInt($('#limit').val());
        const allEdgesChecked = false;
        // const allEdgesChecked = $('#all_edges').is(':checked');

        // alert
        if (!inputNode) {
            alert("Please enter a gene name.");
            return;
        }

        // Clear any existing graph
        document.getElementById('cy').innerHTML = '';

        // Fetch the subgraph data from Flask server
        try {
            // const response = await fetch(`http://127.0.0.1:5000/getNodeData?name=${inputNode}&min_weight=${minWeight}&min_samples=${sampleMinimum}&oncogenes=${oncogenesChecked}&all_edges=${allEdgesChecked}`);
            const response = await fetch(`/coamplification-graph/visualizer/${inputNode}/?min_weight=${minWeight}&min_samples=${sampleMinimum}&oncogenes=${oncogenesChecked}&all_edges=${allEdgesChecked}`);
            if (!response.ok) {
                throw new Error(`Node ${inputNode} not found or server error.`);
            }

            const data = await response.json();

            // Store the complete data for later use in CSV export
            completeData = data;

            total_data = data.nodes.length;
            const filtered_data = filterData(data, limit)
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

            // Update significant class
            document.getElementById('sigThreshold').addEventListener('input', function() {
            const threshold = parseFloat(this.value);
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
        });
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
                
                // Update the gene name in the dropdown menu
                updateGeneNameDisplay(inputNode);
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

            let rownumber = 0;

            cy.nodes().forEach(node => {
                row = document.createElement('tr');

                rownumber_element = document.createElement('td');
                rownumber_element.textContent = rownumber;

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

                cellWeight = document.createElement('td');
                edges = node.edgesWith(cy.$(nodeID[inputNode]));
                cellWeight.textContent = String(edges[0]?.data('weight').toFixed(3) ?? 'N/A');

                row.appendChild(rownumber_element);
                row.appendChild(cellName);
                row.appendChild(cellStatus);
                row.appendChild(cellWeight);
                

                datacontainer.appendChild(row);

                rownumber++;

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

    // Update graph based on chosen signficance test
    document.querySelectorAll('input[name="sigTest"]').forEach(radio => {
        radio.addEventListener('change', () => {
            document.getElementById('sigThreshold').dispatchEvent(new Event('input'));
        });
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
    function sortTable(columnIndex) {
        const table = document.getElementById('data-table');
        const tbody = document.getElementById('data-container');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const noDataRow = tbody.querySelector('.no-data');
        // Remove the "No data available" row temporarily if present
        if (noDataRow) rows.splice(rows.indexOf(noDataRow), 1);
        // Toggle sort order
        let sortOrder = table.dataset.sortOrder === 'asc' ? 'desc' : 'asc';
        table.dataset.sortOrder = sortOrder;
        // Sort rows based on the content of the selected column
        rows.sort((a, b) => {
            const cellA = a.children[columnIndex].innerText.trim();
            const cellB = b.children[columnIndex].innerText.trim();
            if (!isNaN(cellA) && !isNaN(cellB)) {
                // Numeric sort
                return sortOrder === 'asc' ? cellA - cellB : cellB - cellA;
            } else {
                // Text sort
                return sortOrder === 'asc'
                    ? cellA.localeCompare(cellB)
                    : cellB.localeCompare(cellA);
            }
        });
        let rownumber = 1;
        // Re-add sorted rows to the tbody
        rows.forEach(
            row => {
                row.children[0].innerText = rownumber;
                tbody.appendChild(row);
                rownumber++;
            }
        )
        // Re-add the "No data available" row if needed
        if (noDataRow && rows.length === 0) tbody.appendChild(noDataRow);
    }

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

        // Combine and format
        const finalRows = [queryRow, ...otherRows];
        finalRows.forEach(row => {
            const formattedRow = [
                row.gene,
                row.oncogene,
                row.sample_count,
                row.inter_count,
                row.weight === -1 ? 'N/A' : row.weight.toFixed(3),
                row.location,
                row.distance === -1 ? 'N/A' : row.distance,
                row.pval_single_interval === -1 ? 'N/A' : parseFloat(row.pval_single_interval).toFixed(3),
                row.qval_single_interval === -1 ? 'N/A' : parseFloat(row.qval_single_interval).toFixed(3),
                row.odds_ratio_single_interval === -1 ? 'N/A' : parseFloat(row.odds_ratio_single_interval).toFixed(3),
                row.pval_multi_interval === -1 ? 'N/A' : parseFloat(row.pval_multi_interval).toFixed(3),
                row.qval_multi_interval === -1 ? 'N/A' : parseFloat(row.qval_multi_interval).toFixed(3),
                row.odds_ratio_multi_interval === -1 ? 'N/A' : parseFloat(row.odds_ratio_multi_interval).toFixed(3),
                row.pval_multi_chromosomal === -1 ? 'N/A' : parseFloat(row.pval_multi_chromosomal).toFixed(3),
                row.qval_multi_chromosomal === -1 ? 'N/A' : parseFloat(row.qval_multi_chromosomal).toFixed(3),
                row.odds_ratio_multi_chromosomal === -1 ? 'N/A' : parseFloat(row.odds_ratio_multi_chromosomal).toFixed(3),
                row.gene_samples,
                row.inter,
            ].map(formatCell);

            csv.push(formattedRow.join(','));
        });

        return csv.join('\n');
    }

    // Add event listener for the download gene CSV button
    // Single event listener for Download Gene CSV button
    document.addEventListener('click', function(e) {
        if (e.target && e.target.id === 'download-gene-csv') {
            e.preventDefault();
            
            // Make sure completeData and inputNode are available
            if (!completeData || !inputNode) {
                alert('No graph data available. Please load a graph first.');
                return;
            }

            // Generate CSV content directly using the proper parameters
            const csvContent = generateCSV(completeData, inputNode);

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
        }
    });
});