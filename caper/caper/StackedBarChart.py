import time
import pandas as pd
import plotly.express as px

def StackedBarChart(sample, fa_cmap):
    start_time = time.time()
    df = pd.DataFrame(sample)
    classes = ['ecDNA', 'BFB', 'Complex non-cyclic', 'Complex-non-cyclic', 'Linear amplification', 'Linear']

    seen_classes = set(df['Classification'])
    if None in seen_classes:
        seen_classes.remove(None)

    none_samps = set(df[df['AA_amplicon_number'].isna()]['Sample_name'])

    df2 = df.groupby(['Sample_name','Classification'])['Classification'].count().reset_index(name='Count')
    for x in set(classes).difference(seen_classes):
        df2.loc[len(df2)] = [df2['Sample_name'][0], x, 0]

    for x in none_samps:
        df2.loc[len(df2)] = [x, "Linear amplification", 0]

    output = df2.pivot(index='Sample_name', columns='Classification', values='Count')
    df = output.sort_values(classes, ascending=[False, False, False, False, False, False])
    df2 = df.reset_index()
    df2['Sample_name'] = df2['Sample_name'].apply(lambda x: x[0:10] + "..." if len(x) > 10 else x)

    if len(df2['Sample_name']) < 10:
        fig = px.bar(df2, x="Sample_name", y = classes,
                barmode = 'stack',
                 color_discrete_map = fa_cmap,
                 hover_data={'Sample_name': False})
    else:
        fig = px.bar(data_frame = df2, x="Sample_name", y = classes,
                barmode = 'stack',
                 color_discrete_map = fa_cmap,
                 hover_data = {'Sample_name': False}, range_x=([-0.5, 24]))

    fig.update_xaxes(tickangle=60, automargin=True, tickfont=dict(size=10), gridcolor = 'white',
                     rangeslider_visible=True, tickprefix = "   ")
    fig.update_yaxes(gridcolor = 'white', rangemode='tozero', ticks = 'outside') ## ADD Y TICKS
    fig.update_layout(showlegend=False, plot_bgcolor = 'white')
    fig.update_layout(yaxis_title="Number of focal amps")
    fig.update_layout(xaxis_title=None)
    fig.update_layout(height=400, margin={'t': 20, 'b': 0, 'r': 0, 'l': 20})
    fig.update_traces(hovertemplate='%{y}: amps, ' + 'Sample: %{x}' + '<br><b></b>', cliponaxis = True)
    
    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Created project barchart plot in {elapsed_time} seconds")
    return fig.to_html(full_html=False, config={'displayModeBar': ['True']}, #'modeBarButtonsToRemove': ['zoom'],
                       div_id="project_bar_plotly_div")
