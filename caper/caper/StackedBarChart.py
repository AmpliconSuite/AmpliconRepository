from collections import defaultdict
import time
import pandas as pd
import plotly.express as px

def StackedBarChart(sample):
    start_time = time.time()
    df = pd.DataFrame(sample)
    classes = ['ecDNA', 'BFB', 'Complex non-cyclic', 'Linear amplification']
    corder = {'ecDNA':0, 'BFB': 1, 'Complex-non-cyclic':2, 'Complex non-cyclic':3, 'Linear amplification':4, 'Linear':5}

    seen_classes = set(df['Classification'])
    if None in seen_classes:
        seen_classes.remove(None)

    none_samps = set(df[df['AA_amplicon_number'].isna()]['Sample_name'])

    df2 = df.groupby(['Sample_name','Classification'])['Classification'].count().reset_index(name='Count')
    for x in set(classes).difference(seen_classes):
        df2.loc[len(df2)] = [df2['Sample_name'][0], x, 0]

    for x in none_samps:
        df2.loc[len(df2)] = [x, "Linear amplification", 0]

    class_count_per_sample = defaultdict(lambda: defaultdict(int))
    for _, row in df2.iterrows():
        class_count_per_sample[row['Sample_name']][row['Classification']] = row['Count']

    cc_tuples = {x: [-y[c] for c in classes] for x, y in class_count_per_sample.items()}
    sort_col = [(corder[row['Classification']], cc_tuples[row['Sample_name']], row['Sample_name']) for _, row in df2.iterrows()]
    df2['sort_order_col'] = sort_col

    df2.sort_values(inplace=True, by=['sort_order_col'])
    #output = df2.pivot(index='Sample_name', columns='Classification', values='Count')
    #df = output.sort_values(classes, ascending=[False, False, False, False])
    # df2 = df.reset_index()
    df2['Sample_name_trunc'] = df2['Sample_name'].apply(lambda x: x[0:10] + "..." if len(x) > 10 else x)

    if len(df2['Sample_name']) < 10:
        fig = px.bar(df2, x="Sample_name", y = "Count", color='Classification',
                barmode = 'stack', custom_data=["Sample_name", "Classification"],
                color_discrete_map = {
                        'ecDNA' : "rgb(255, 0, 0)",
                        'BFB' : 'rgb(0, 70, 46)',
                        'Complex non-cyclic' : 'rgb(255, 190, 0)',
                        'Linear amplification' : 'rgb(27, 111, 185)'},
                )
    else:
        fig = px.bar(df2, x="Sample_name", y = "Count", color='Classification',
                barmode = 'stack', custom_data=["Sample_name", "Classification"], range_x=([-0.5, 24]),
                color_discrete_map = {
                        'ecDNA' : "rgb(255, 0, 0)",
                        'BFB' : 'rgb(0, 70, 46)',
                        'Complex non-cyclic' : 'rgb(255, 190, 0)',
                        'Linear amplification' : 'rgb(27, 111, 185)'},
                )

    showslider = True if len(df2['Sample_name']) > 24 else False
    fig.update_xaxes(tickangle=60, automargin=True, tickfont=dict(size=10), gridcolor = 'white',
                     rangeslider_visible=showslider, tickprefix = "  ")
    fig.update_yaxes(gridcolor = 'white', rangemode='tozero', ticks = 'outside') ## ADD Y TICKS
    fig.update_traces(hovertemplate=
                      "<b>%{customdata[0]}</b><br>" +
                      "Class: %{customdata[1]}<br>" +
                      "Count: %{y}<br>" +
                      "<extra></extra>",
                      )
    fig.update_layout(showlegend=False, plot_bgcolor = 'white', yaxis_title="Number of focal amps", xaxis_title=None,
                      height=400, margin={'t': 20, 'b': 0, 'r': 0, 'l': 20},
                      xaxis={
                        'tickmode': 'array',
                        'tickvals': list(range(len(df2['Sample_name']))),
                        'ticktext': df2['Sample_name_trunc'].tolist(),
                      },
    )

    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Created project barchart plot in {elapsed_time} seconds")
    return fig.to_html(full_html=False, config={'displayModeBar': ['True']}, #'modeBarButtonsToRemove': ['zoom'],
                       div_id="project_bar_plotly_div")
