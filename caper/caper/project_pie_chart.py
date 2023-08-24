import pandas as pd
import plotly.express as px

def pie_chart(sample, fa_cmap):
    df = pd.DataFrame(sample)

    class_counts = df['Classification'].value_counts()
    # color_scale_map = {
    #     'ecDNA': '#FF4343',
    #     'Complex non-cyclic': '#FFBE00',
    #     'BFB' :'#00462E',
    #     'Linear amplification': '#1B6FB9',
    #     'Complex-non-cyclic': '#FFBE00',
    #     'Linear': '#1B6FB9'
    # }
    fig = px.pie(class_counts, values='Classification', color=class_counts.index, names=class_counts.axes[0].tolist(),
                 color_discrete_map= fa_cmap)
    fig.update_layout(font = dict( size=12),
                width=400,
                height=400,
                autosize = False,
                margin=dict(l=15, r=15, t=15, b= 15),
                showlegend = True, legend_itemclick=False)

    fig.update_layout(
        title_text="Percentage of all focal amps", title_y=0.01, title_x=0.5,
        legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        xanchor="right",
        x=0.96
    ))
    fig.update_traces(textfont_size = 12, textposition='inside', hovertemplate= '<b>%{label}</b><br>%{value} Focal Amps')
    fig.update_layout(margin={'t': 0, 'b': 40})

    updated_config_dict = {'displayModeBar': True, 'displaylogo':False,
                           'toImageButtonOptions': {
                               'format': 'svg',  # one of png, svg, jpeg, webp
                            }
                           }

    return fig.to_html(full_html=False, config=updated_config_dict, div_id='project_pie_plotly_div')
    