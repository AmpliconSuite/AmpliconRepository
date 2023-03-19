import os
from matplotlib.pyplot import autoscale
import pandas as pd
import plotly.express as px
import matplotlib.cm as cm

def pie_chart(directory):
#directory = '/mnt/C/Users/ahuja/Desktop/bafna_lab/AABeautification/AA_outputs/'
    df = pd.DataFrame()
    for folder in os.listdir(directory):
        classFolder = folder+('_classification')
        resultFile = folder+('_result_table.tsv')
        f = os.path.join(directory, folder, classFolder, resultFile)
        if os.path.isfile(f):
            tsv = pd.read_csv(f, sep = '\t')
            df = pd.concat([df, tsv])
    class_counts = df['Classification'].value_counts()
    color_scale_map = {
        'ecDNA': '#FF4343',
        'Complex non-cyclic' : '#FFBE00' , 
        'BFB' :'#00462E', 
        'Linear amplification': '#1B6FB9'
    }
    fig = px.pie(class_counts, values= 'Classification', color=class_counts.index, names=class_counts.axes[0].tolist(), title='Project Classification Count Distribution', color_discrete_map= color_scale_map)
    fig.update_layout(font = dict( size=12, color="black"),
                width=400,
                height=400,
                autosize = False,
                margin=dict(l=15, r=15, t=50, b= 0),
                showlegend = False)
    fig.update_traces(textfont_size = 12)
    fig.update_traces(textposition='inside', textinfo='percent+value')

    return fig.to_html(full_html=False, div_id='project_pie_plotly_div')
    