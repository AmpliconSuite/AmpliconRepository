import time
import pandas as pd
import plotly.express as px


def StackedBarChart(sample):
    start_time = time.time()
    df = pd.DataFrame(sample)
    classes = ['ecDNA', 'BFB', 'Complex non-cyclic', 'Linear amplification']

    seen_classes = set(df['Classification'])
    if None in seen_classes:
        seen_classes.remove(None)

    none_samps = set(df[df['AA amplicon number'].isna()]['Sample name'])

    df2 = df.groupby(['Sample name','Classification'])['Classification'].count().reset_index(name='Count')
    for x in set(classes).difference(seen_classes):
        df2.loc[len(df2)] = [df2['Sample name'][0], x, 0]

    for x in none_samps:
        df2.loc[len(df2)] = [x, "Linear amplification", 0]

    output = df2.pivot(index='Sample name', columns='Classification', values='Count')
    df = output.sort_values(classes, ascending=[False, False, False, False])
    df2 = df.reset_index()

    fig = px.bar(df2, x="Sample name", y = classes,
                barmode = 'stack',
                 color_discrete_map = {
                        'ecDNA' : "rgb(255, 0, 0)",
                        'BFB' : 'rgb(0, 70, 46)',
                        'Complex non-cyclic' : 'rgb(255, 190, 0)',
                        'Linear amplification' : 'rgb(27, 111, 185)'},
                 hover_data = {'Sample name': False})

    fig.update_xaxes(tickangle=90, automargin=False, tickfont=dict(size=10), gridcolor = 'white')
    fig.update_yaxes(gridcolor = 'white')
    fig.update_layout(showlegend=False, plot_bgcolor = 'white')
    fig.update_layout(yaxis_title="Number of focal amps")
    fig.update_layout(xaxis_title=None)
    fig.update_layout(height=400, margin={'t': 20, 'b': 80, 'r': 0, 'l': 20})
    fig.update_traces(hovertemplate='%{y:} amps, ' + 'Sample: %{x}' + '<br><b></b>')

    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Created project barchart plot in {elapsed_time} seconds")
    return fig.to_html(full_html=False, config={'modeBarButtonsToRemove': ['zoom'], 'displayModeBar':False},
                       div_id="project_bar_plotly_div")
