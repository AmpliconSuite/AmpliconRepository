import pandas as pd
import plotly.express as px

def pie_chart(sample):
    df = pd.DataFrame(sample)

    class_counts = df['Classification'].value_counts()
    color_scale_map = {
        'ecDNA': '#FF4343',
        'Complex non-cyclic' : '#FFBE00' , 
        'BFB' :'#00462E', 
        'Linear amplification': '#1B6FB9'
    }
    fig = px.pie(class_counts, values= 'Classification', color=class_counts.index, names=class_counts.axes[0].tolist(),
                 color_discrete_map= color_scale_map)
    fig.update_layout(font = dict( size=12, color="black"),
                width=400,
                height=400,
                autosize = False,
                margin=dict(l=15, r=15, t=15, b= 15),
                showlegend = True, legend_itemclick=False)
    fig.update_layout(title_text="Percentage of all focal amps", title_y=0.01, title_x=0.5)

    # fig.add_annotation(x=0.5, y=0.9, text="Proportion of all focal amps", showarrow=False)

    fig.update_layout(legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        xanchor="right",
        x=1
    ))
    fig.update_traces(textfont_size = 12)
    fig.update_traces(textposition='inside', hovertemplate= "<br> %{label} </br> %{percent}")
    fig.update_layout(margin={'t': 0, 'b': 40})

    return fig.to_html(full_html=False, config={'displayModeBar': False}, div_id='project_pie_plotly_div')
    