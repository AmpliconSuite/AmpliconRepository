from statistics import mode
from dash import Dash, html, dcc
import plotly.express as px
import dash_bio as dashbio
import pandas as pd
import plotly.graph_objs as go
import numpy as np
from numpy import random
import warnings
from plotly.subplots import make_subplots
from pylab import cm
import os
import re

cent_file = 'bed_files/GRCh38_centromere.bed'
warnings.filterwarnings("ignore")


def plot(sample, sample_name, project_name, filter_plots=True):
    project_data_dir = f'project_data/{project_name}/extracted'
    if not os.path.exists(project_data_dir):
        return ''

    CNV_file = sample[0]['CNV BED file']
    CNV_file = CNV_file[CNV_file.index('AA_outputs'):]
    CNV_file = f"{project_data_dir}/{CNV_file}"
    amplicon = pd.DataFrame(sample)

    amplicon['Oncogenes'] = amplicon['Oncogenes'].str.replace('[', '')
    amplicon['Oncogenes'] = amplicon['Oncogenes'].str.replace("'", "")
    amplicon['Oncogenes'] = amplicon['Oncogenes'].str.replace(']', '')
    amplicon['Location'] = amplicon['Location'].str.replace('[', '')
    amplicon['Location'] = amplicon['Location'].str.replace("'", "")
    amplicon['Location'] = amplicon['Location'].str.replace(']', '')
    
    valid_range = lambda loc: int(loc[1]) - int(loc[0]) > 1000000
    valid_amp = lambda x: any(valid_range(loc.split(':')[1].split('-')) for loc in x['Location'].split(', '))
    valid_amp_df = lambda df: [valid_amp(row) for row in df.iloc]
    amplicon = amplicon[valid_amp_df(amplicon)]
    amplicon_numbers = list(amplicon['AA amplicon number'].unique())
    seen = set()
    
    chr_order = lambda x: int(x) if x.isnumeric() else ord(x)
    if filter_plots:
        chromosomes = list(sorted(set(loc.split(':')[0].lstrip('chr') for loc in (', '.join(amplicon['Location'])).split(', ') if valid_range(loc.split(':')[1].split('-'))), key=chr_order))
    else:
        chromosomes = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11",
        "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22",
        "X", "Y")
    #amplicon = pd.DataFrame(sample)
    
    cmap = cm.get_cmap('Spectral', len(amplicon['AA amplicon number'].unique()))
    amplicon_colors = [f"rgba({', '.join([str(val) for val in cmap(i)])})" for i in range(cmap.N)]

    rows = (len(chromosomes) // 4) + 1 if len(chromosomes) % 4 else len(chromosomes) // 4
    fig = make_subplots(rows=rows, cols=4
        ,subplot_titles=chromosomes, horizontal_spacing=0.05, vertical_spacing = 0.07)

    df = pd.read_csv(CNV_file, sep = "\t", header = None)
    df.rename(columns = {0: 'Chromosome Number', 1: "Feature Start Position", 2: "Feature End Position", 3: 'CNV Gain', 4: 'Copy Number'}, inplace = True)
    dfs = {}
    
    
    for chromosome in df['Chromosome Number'].unique():
        key = chromosome
        value = df[df['Chromosome Number'] == chromosome]
        dfs[key] = value

    ## CREATE ARRAY

    rowind = 1
    colind = 1

    for key in (chromosomes if filter_plots else dfs):
        if filter_plots:
            key = 'chr' + key
        log_scale = False
        copyNumberdf = pd.DataFrame()
        x_array = []
        y_array = []
        for i in range(len(dfs[key])):
            x_array.append(dfs[key].iloc[i, 1])
            x_array.append(dfs[key].iloc[i, 2])
            y_array.append(dfs[key].iloc[i, 4])
            y_array.append(dfs[key].iloc[i, 4])
            x_array.append(dfs[key].iloc[i, 2])
            y_array.append(np.nan)


        x_array = [round(item, 2) for item in x_array]
        y_array = [round(item, 2) for item in y_array]
        fig.add_trace(go.Scatter(x=x_array,y=y_array,mode = 'lines', name="Copy Number", showlegend = False, line = dict(color = 'green')), row = rowind, col = colind)

        amplicon_df = pd.DataFrame()
        for i in range(len(amplicon)):
            row = amplicon.iloc[[i]]
            loc = row.iloc[0, 4]
            splitloc = loc.split(',')
            for element in splitloc:
                chrsplit = element.split(':')
                chr = chrsplit[0]
                if chr == key or chr[1:] == key:
                    for j in range(0,2):
                        row['Chromosome Number'] = chrsplit[0]
                        locsplit = chrsplit[1].split('-') 
                        row['Feature Start Position'] = int(locsplit[0])
                        row['Feature End Position'] = int(locsplit[1])
                        if int(locsplit[1]) - int(locsplit[0]) < 5000000:
                            if j == 0:
                                row['Feature Position'] = locsplit[0]
                                row['Y-axis'] = 95
                            elif j == 1:
                                row['Feature Position'] = int(locsplit[1]) + 3000000
                                row['Y-axis'] = 95
                            amplicon_df = pd.concat([row, amplicon_df])
                        else:
                            if j == 0:
                                row['Feature Position'] = locsplit[0]
                                row['Y-axis'] = 95
                            elif j == 1:
                                row['Feature Position'] = locsplit[1]
                                row['Y-axis'] = 95
                            amplicon_df = pd.concat([row, amplicon_df])

                            
        if not amplicon_df.empty:
            amplicon_df['Feature Start Position'] = amplicon_df['Feature Start Position'].astype(float)
            amplicon_df['Feature End Position'] = amplicon_df['Feature End Position'].astype(float)
            amplicon_df['Feature Position'] = amplicon_df['Feature Position'].astype(float)
            amplicon_df['Feature Maximum Copy Number'] = amplicon_df['Feature maximum copy number'].astype(float)
            amplicon_df['Feature Median Copy Number'] = amplicon_df['Feature median copy number'].astype(float)
            amplicon_df = amplicon_df.round(decimals=2)
            for i in range(len(amplicon_df['AA amplicon number'].unique())):
                number = amplicon_df['AA amplicon number'].unique()[i]
                per_amplicon = amplicon_df[amplicon_df['AA amplicon number'] == number]
                
                show_legend = number not in seen
                seen.add(number)
                
                fig.add_trace(go.Scatter(x = per_amplicon['Feature Position'], y = per_amplicon['Y-axis'], 
                        customdata = amplicon_df, mode='lines',fill='tozeroy', hoveron='points+fills', hovertemplate=
                        '<br><i>Feature Classification:</i> %{customdata[3]}<br>' + 
                        '<i>%{customdata[16]}:</i> %{customdata[17]} - %{customdata[18]}<br>' +
                        '<i>Oncogenes:</i> %{customdata[5]}<br>'+
                        '<i>Feature Maximum Copy Number:</i> %{customdata[9]}<br>'
                        f'<b class="/{project_data_dir}/AA_outputs/{sample_name}/{sample_name}_AA_results/{sample_name}_amplicon{number}.png">Click to Download Amplicon {number} PNG</b>',
                        name = '<b>Amplicon ' + str(number) + '</b>', opacity = 0.3, fillcolor = amplicon_colors[amplicon_numbers.index(number)], 
                        line = dict(color = amplicon_colors[amplicon_numbers.index(number)]), showlegend=show_legend, legendrank=number), row = rowind, col = colind)

        cent_df = pd.read_csv(cent_file, header = None, sep = '\t')
        #display(a_df)
        cent_df = cent_df[cent_df[0] == key]
        #display(cent_df)
        chr_df = pd.DataFrame()
        for i in range(len(cent_df)):
            row = cent_df.iloc[[i]]
            for j in range(0, 2):
                if j == 0:
                    row['Centromere Position'] = row.iloc[0, 1]
                    row['Y-axis'] = 95
                elif j == 1:
                    row['Centromere Position'] = row.iloc[0, 2]
                    row['Y-axis'] = 95
                chr_df = pd.concat([row, chr_df])
                    


        if rowind == 1 and colind == 1:
            fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)', 
                line_color = 'rgba(2, 6, 54, 0.3)', customdata = chr_df, hovertemplate = 
                '<br>%{customdata[0]}: %{customdata[1]}-%{customdata[2]}', name = 'Centromere', legendrank=0), row = rowind, col = colind)
        else:
            fig.add_trace(go.Scatter(x = chr_df['Centromere Position'], y = chr_df['Y-axis'], fill = 'tozeroy', mode = 'lines', fillcolor = 'rgba(2, 6, 54, 0.3)', 
                line_color = 'rgba(2, 6, 54, 0.3)', customdata = chr_df, name = 'Centromere', showlegend = False, hovertemplate = 
                '<br>%{customdata[0]}: %{customdata[1]}-%{customdata[2]}'
                ), row = rowind, col = colind)

        fig.update_xaxes(showline = True, linewidth = 1, title_font_size = 10,  ticksuffix = " ")

        #print(y_array)
        for element in y_array:
            if element > 20:
                log_scale = True

        if log_scale:
            fig.update_yaxes(autorange = False, type="log", ticks = 'outside', ticktext = ['0','1', '', '', '', '', '', '', '', '', '10', '100'], 
                ticklen = 10, showline = True, linewidth = 1, showgrid = False, range = [0,2], tick0 = 0, dtick = 1, tickmode = 'array', tickvals = [0,1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100], 
                ticksuffix = " ", row = rowind, col = colind)
        else:
            fig.update_yaxes(autorange = False, ticks = 'outside', ticklen = 10, range = [0, 20], ticktext = ['0', '', '', '', '20'], tickvals = [0, 5, 10, 15, 20], showline = True, linewidth = 1, showgrid = False, 
                tick0 = 0, dtick = 1, tickmode = 'array', ticksuffix = " ", row = rowind, col = colind)

        if colind == 1:
            fig.update_yaxes(title = 'Copy Number', row = rowind, col = colind)

        if colind == 4:
            rowind += 1
            colind = 1
        else:
            colind += 1


    height = {
        1: 250,
        2: 500,
        3: 600,
        4: 750,
        5: 900,
        6: 1000
    }

    fig.update_layout(title_font_size=30,  
    xaxis = dict(gridcolor='white'), template = None, hovermode = 'x unified', title_text=f"{sample_name} Copy Number Plots",
    height = height[rows], width = 1300, margin = dict(t = 70, r = 70, b = 70, l = 70))

    plot_div = fig.to_html(full_html=False)

    # https://community.plotly.com/t/hyperlink-to-markers-on-map/17858/6
    # Get id of html div element that looks like
    # <div id="301d22ab-bfba-4621-8f5d-dc4fd855bb33" ... >
    res = re.search('<div id="([^"]*)"', plot_div)
    div_id = res.groups()[0]
    # Build JavaScript callback for handling clicks
    # and opening the URL in the trace's customdata 
    js_callback = """
    <script>
    var plot_element = document.getElementById("{div_id}");
    plot_element.on('plotly_click', function(data){{
        var link = '';
        for (let i = 0; i < data['points'].length; i++) {{
            var name = data['points'][i]['data']['name'];
            if (name.includes('Amplicon')) {{
                link = data['points'][i]['data']['hovertemplate'].split('"')[1];
                break;
            }}
        }}

        if (link != '') {{
            var link_window = document.getElementById("figure_download_window");
            var link_elem = link_window.firstChild;
            link_elem.href = link;

            var preview_elem = link_elem.lastChild;
            preview_elem.setAttribute('src', link);

            if (link_elem.firstChild.tagName != "B") {{
                link_elem.innerHTML = '<b style="text-decoration: underline">Download ' + name.slice(3, -4) + ' PNG</b>' + link_elem.innerHTML;
            }}
            else {{
                link_elem.firstChild.innerHTML = 'Download ' + name.slice(3, -4) + ' PNG';
            }}
            link_window.setAttribute('style', 'display: flex; align-items: center; margin-bottom: 2rem');
        }}
    }})

    var closebtn = document.getElementById("close");
    closebtn.addEventListener("click", function() {{
        this.parentElement.style.display = 'none';
    }});

    $('#toggle-event').change(function() {{
        const current_link = window.location.href.split('?');
        if (current_link.length == 1) {{
            window.location.href = current_link[0] + '?display_all_chr=T';
        }}
        else {{
            window.location.href = current_link[0];
        }}
    }});
    
    </script>
    """.format(div_id=div_id)

    # Build HTML string
    html_str = """
    <div id="figure_download_window", style='display: none'><a href='' target='_blank' download="download" style='border-style: solid; padding: 0.1rem 0.5rem; border-width: 0.15rem; border-color: black; display: flex; flex-flow: column'><br><img src='' style='width:30rem'></a><span id='close' style='margin-left: 5px; color: grey; font-size: 1.5rem'>&times</span></div>
    
    <link href="https://cdn.jsdelivr.net/gh/gitbrent/bootstrap4-toggle@3.6.1/css/bootstrap4-toggle.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/gh/gitbrent/bootstrap4-toggle@3.6.1/js/bootstrap4-toggle.min.js"></script>
    <input id="toggle-event" type="checkbox" class="form-check-input" data-toggle="toggle">
    <span style='margin-left: 1rem'> Display All Chromosomes </span>
    <script>
    if (window.location.href.split('?').length != 1) {{
        var toggle = document.getElementById("toggle-event");
        toggle.setAttribute('checked', 'True');
    }}
    </script>
    {plot_div}
    {js_callback}
    """.format(plot_div=plot_div, js_callback=js_callback)
    return html_str

