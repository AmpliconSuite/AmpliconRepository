window.addEventListener('DOMContentLoaded', function () {
    // Global variables
    let cy = null
    let nodeID = {};
    let allTooltips = {};
    let inputNode = null
    let total_data = 0;
    console.log(document.styleSheets)

    cytoscape.use( cytoscapePopper(tippyFactory) );
    
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
            total_data = data.nodes.length;
            // const filtered_data = filterData(data, limit)
            // Initialize Cytoscape with fetched data
            cy = cytoscape({
                container: document.getElementById('cy'),
                elements: data,  // Use the data from the server
                style: [
                    { selector: 'node', style: { 'background-color': '#A7C6ED', 'label': '' } },
                    { selector: `node[label="${inputNode}"], node.highlighted`, style: {'z-index': 100, 'label': 'data(label)' } }, //, 'border-width': 2, 'border-color': 'black', 'border-style': 'solid' } },
                    { selector: `node[oncogene="True"]`, style: { 'background-color': '#ff4757', 'z-index': 10, 'label': 'data(label)' } },
                    { selector: 'edge', style: { 'width': 2, 'line-color': '#ccc' } },
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

    // document.addEventListener('DOMContentLoaded', function () {
    //     cytoscape.use( cytoscapePopper(tippyFactory) );
    // });

    // Set tooltip content
    function createTooltipContent(ele) {
        let content = '';
        if (ele.isNode()) {
            let template = document.getElementById('node-template');
            template.querySelector('#ntip-name').textContent = ele.data('label') || 'N/A';
            template.querySelector('#ntip-oncogene').textContent = ele.data('oncogene') || 'N/A';
            template.querySelector('#ntip-nsamples').textContent = ele.data('features').length || 'N/A';
            template.querySelector('#ntip-samples').textContent = ele.data('features').join(', ') || 'N/A';
            content = template.innerHTML;
        }
        else {
            let template = document.getElementById('edge-template');
            template.querySelector('#etip-name').textContent = ele.data('label') || 'N/A';
            template.querySelector('#etip-weight').textContent = ele.data('weight').toFixed(3) || 'N/A';
            template.querySelector('#etip-frac').textContent = ele.data('leninter') + '/' + ele.data('lenunion');
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
        // const center = cy.nodes(`[name = "${input}"]`);
        cy.nodes().forEach(node => {
            if (node.data('label') === input) {
                const size = radius*(1.5);
                node.style({ 'width': size, 'height': size });
                node.data('size', size);
            }
            else {
                const edges = node.edgesWith(cy.$(nodeID[input]));
                const scale = edges.reduce((sum, edge) => sum + edge.data('weight'), 0);
                const size = radius*(scale);
                node.style({ 'width': size, 'height': size });
                node.data('size', size);
            }
        });
        cy.layout({
            name: 'fcose',
            gravity: 1.5,               // Higher gravity pulls larger nodes more centrally
            gravityRange: 1.0,          // Smaller range keeps nodes closer to center
            idealEdgeLength: (edge) => {
            // Larger nodes = edge length, closer to the center
            const sourceSize = edge.source().data('size');
            const targetSize = edge.target().data('size');
            return 100 - 1.5*Math.min(sourceSize, targetSize);
            },
            nodeRepulsion: (node) => {
            // Larger nodes have lower repulsion to stay closer
            return 4500 - 100*node.data('size');
            },
            animate: true,
            animationDuration: 700
        }).run();
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

    // update sliders
    document.getElementById('edgeWeight').addEventListener('input', function() {
        document.getElementById('sliderValue').textContent = this.value;
    });
    document.getElementById('numSamples').addEventListener('input', function() {
        document.getElementById('sampleValue').textContent = this.value;
    });
    const slider = document.getElementById('limit');
    const tooltip = document.getElementById('sliderTooltip');

    slider.addEventListener('input', function () {
        // Calculate percentage position
        const value = ((slider.value - slider.min) / (slider.max - slider.min)) * 100;

        // Update tooltip text
        tooltip.textContent = slider.value;
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
            document.getElementById('queryResult').textContent = total_data;
            document.getElementById('limit').max = total_data;
            document.getElementById('limitMaxText').textContent = total_data;
            document.getElementById('sliderTooltip').textContent = total_data;
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
        if (cellContent.startsWith('[') && cellContent.endsWith(']')) {
            // For formatting, need lists to be formatted as lists in csv and not a string
            return `"${cellContent.replace(/"/g, '""')}"`; 
        }
        return cellContent;
    }

    // Function to generate CSV
    function generateCSV(datacontainercsv) {    
        if (!datacontainercsv) {
            alert('Data container is not available.');
            return '';
        }
        const rows = Array.from(datacontainercsv.querySelectorAll('tr'));
        const csv = [];

        // Add header row to CSV
        const header = ['Gene Name', 'Oncogene', 'Coamplification Frequency', 'Intersection Samples', 'Union Samples'];
        csv.push(header.join(',')); // Join column labels with commas

        
        // Separate the first row
        const firstRow = rows.shift(); // Remove the first row (query gene needs to be at top for reference)
        const firstRowData = Array.from(firstRow.querySelectorAll('td')).map(cell => {
            return formatCell(cell.textContent);
        });
        csv.push(firstRowData.join(','));

        // Sort remaining rows by `cell.weight` in descending order
        const sortedRows = rows.sort((a, b) => {
            const weightA = parseFloat(a.querySelector('td:nth-child(3)').textContent) || 0;
            const weightB = parseFloat(b.querySelector('td:nth-child(3)').textContent) || 0;
            return weightB - weightA;
        });

        // Process sorted rows
        sortedRows.forEach(row => {
            const cols = Array.from(row.querySelectorAll('td')).map(cell => {
                return formatCell(cell.textContent);
            });
            csv.push(cols.join(','));
        });

        return csv.join('\n'); 
    }

    // Add event listener for the download button
    document.getElementById('download-btn').addEventListener('click', () => {
        const datacontainercsv = document.createElement('table');
        // Generate a table from cytoscape graph for export
        cy.nodes().forEach(node => {
            row = document.createElement('tr');

            const cellName = document.createElement('td');
            const geneName = node.data('label');
            cellName.textContent = geneName

            cellStatus = document.createElement('td');
            cellStatus.textContent = node.data('oncogene');

            cellWeight = document.createElement('td');
            cellUnion = document.createElement('td');
            cellInter = document.createElement('td');

            const edges = node.edgesWith(cy.$(nodeID[inputNode]));

            const edgeData = edges[0]?.data() || {};

            cellWeight.textContent = String(edgeData.weight?.toFixed(3) ?? 'N/A');
            const unionList = edgeData.union ? `["${edgeData.union.join('", "')}"]` : 'N/A';
            cellUnion.textContent = unionList;
            
            const interList = edgeData.inter ? `["${edgeData.inter.join('", "')}"]` : 'N/A';
            cellInter.textContent = interList;
            
            row.appendChild(cellName);
            row.appendChild(cellStatus);
            row.appendChild(cellWeight);
            row.appendChild(cellInter);
            row.appendChild(cellUnion);
            datacontainercsv.appendChild(row);

        const csvContent = generateCSV(datacontainercsv);
        const blob = new Blob([csvContent], { type: 'text/csv' });
        const url = URL.createObjectURL(blob);

        // Create a temporary download link
        const link = document.createElement('a');
        link.href = url;
        const now = new Date();
        // Format date and time (e.g., YYYY-MM-DD_HH-MM-SS)
        const formattedDate = now.toISOString().replace(/:/g, '-').replace('T', '_').split('.')[0];
        link.download = `AACoampGraph_${inputNode}_${formattedDate}.csv`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        });
    })
});