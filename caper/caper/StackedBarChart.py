from collections import defaultdict
import logging
import time

import pandas as pd
import plotly.express as px


def StackedBarChart(sample, fa_cmap):
    start_time = time.time()
    df = pd.DataFrame(sample)
    df['Sample_name'] = df['Sample_name'].astype(str)
    corder = {'ecDNA':0, 'BFB': 1, 'Complex-non-cyclic':2, 'Complex non-cyclic':3, 'Linear amplification':4, 'Linear':5, 'Virus':6, 'None':100}
    classes = ['ecDNA', 'BFB', 'Complex non-cyclic', 'Complex-non-cyclic', 'Linear amplification', 'Linear', 'Virus', 'None']

    seen_classes = set(df['Classification'])
    if None in seen_classes:
        seen_classes.remove(None)

    none_samps = set(df[df['AA_amplicon_number'].isna()]['Sample_name'])

    df2 = df.groupby(['Sample_name','Classification'])['Classification'].count().reset_index(name='Count')
    for x in set(classes).difference(seen_classes):
        df2.loc[len(df2)] = [df2['Sample_name'][0], x, 0]

    for x in none_samps:
        df2.loc[len(df2)] = [x, "None", 0]

    class_count_per_sample = defaultdict(lambda: defaultdict(int))
    for _, row in df2.iterrows(): # 1 loop 
        class_count_per_sample[row['Sample_name']][row['Classification']] = row['Count']

    # df2['Sample_name_trunc'] = df2['Sample_name'].apply(lambda x: x[0:10] + "..." if len(x) > 10 else x)
    cc_tuples = {x: [-y[c] for c in classes] for x, y in class_count_per_sample.items()} 
    sort_col = [(corder[row['Classification']], cc_tuples[row['Sample_name']], row['Sample_name']) for _, row in df2.iterrows()] # 1 loop
    df2['sort_order_col'] = sort_col
    df2.sort_values(inplace=True, by=['sort_order_col']) # 1 loop
    ordered_name_set = df2['Sample_name'].unique()

    if len(df2['Sample_name']) < 10:
        fig = px.bar(df2, x="Sample_name", y = "Count", color='Classification',
                barmode = 'stack', custom_data=["Sample_name", "Classification"],
                color_discrete_map = fa_cmap,
            )
    else:
        fig = px.bar(df2, x="Sample_name", y = "Count", color='Classification',
                barmode = 'stack', custom_data=["Sample_name", "Classification"], range_x=([-0.5, min(24, len(ordered_name_set))]),
                color_discrete_map = fa_cmap,
            )

    showslider = True if len(ordered_name_set) > 24 else False
    fig.update_xaxes(tickangle=60, automargin=True, tickfont=dict(size=10), gridcolor = 'white',
                     rangeslider_visible=showslider, tickprefix = "  ")
    fig.update_yaxes(gridcolor = 'white', rangemode='tozero', ticks = 'outside')
    fig.update_traces(hovertemplate=
                      "<b>%{customdata[0]}</b><br>" +
                      "Class: %{customdata[1]}<br>" +
                      "Count: %{y}<br>" +
                      "<extra></extra>",
                      )
    trunc_names = [x[0:10] + "..." if len(str(x)) > 12 else x for x in ordered_name_set]
    fig.update_layout(showlegend=False, plot_bgcolor = 'white', yaxis_title="Number of focal amps", xaxis_title=None,
                      height=400, margin={'t': 20, 'b': 0, 'r': 0, 'l': 20},
                      xaxis={
                        'tickmode': 'array',
                        'tickvals': list(range(len(df2['Sample_name']))),
                        'ticktext': trunc_names,
                      }
    )

    end_time = time.time()
    elapsed_time = end_time - start_time
    updated_config_dict = {'displayModeBar': ['True'],
                           'toImageButtonOptions': {
                               'format': 'svg',  # one of png, svg, jpeg, webp
                            }
                           }
    logging.info(f"Created project barchart plot in {elapsed_time} seconds")
    return fig.to_html(full_html=False, config=updated_config_dict, div_id="project_bar_plotly_div")
