import os
from matplotlib.pyplot import autoscale
import pandas as pd
import plotly.express as px
import matplotlib.cm as cm

def pie_chart(sample):
    #directory = '/mnt/c/Users/ahuja/Desktop/data/AA_outputs/'
    df = pd.DataFrame(sample)
    print(df.columns)
    print(df)
    # for folder in os.listdir(directory):
    #     classFolder = folder+('_classification')
    #     resultFile = folder+('_result_table.tsv')
    #     f = os.path.join(directory, folder, classFolder, resultFile)
    #     if os.path.isfile(f):
    #         tsv = pd.read_csv(f, sep = '\t')
    #         df = pd.concat([df, tsv])

    # d = {'ecDNA': 0, 'BFB': 0, 'Complex non-cyclic': 0, 'Linear amplification': 0}
    
    # for element in df['Classifications']:
    #     if not element == []:
    #         for classif in element:
    #             if classif == 'ECDNA':
    #                d['ecDNA'] += 1
    #             elif classif == 'BFB':
    #                d['BFB'] += 1
    #             elif classif == 'COMPLEX NON-CYCLIC':
    #                 d['Complex non-cyclic'] += 1
    #             elif classif == 'LINEAR AMPLIFICATION':
    #                 d['Linear amplification'] += 1
            
    #class_counts = pd.Series(data=d, index=['ecDNA', 'BFB', 'Complex non-cyclic', 'Linear amplification'])
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
                margin=dict(l=15, r=15, t=15, b= 0),
                showlegend = True, legend_itemclick=False)

    fig.update_layout(legend=dict(
        orientation="h",
        yanchor="bottom",
        y=1.02,
        xanchor="right",
        x=1
    ))
    fig.update_traces(textfont_size = 12)
    fig.update_traces(textposition='inside', textinfo='percent+value')

    return fig.to_html(full_html=False, config={'displayModeBar': False}, div_id='project_pie_plotly_div')
    