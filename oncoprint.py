from tkinter import ON
import dash
from dash.dependencies import Input, Output
import dash_bio as dashbio
from dash import html
import pandas as pd
import dash
import dash_html_components as html
import dash_core_components as dcc
import plotly.graph_objects as go
import plotly.express as px
import json
from custom_oncoprint import Oncoprint
#from dash_bio import OncoPrint

#from custom_oncoprint.custom_oncoprint.Oncoprint import Oncoprint

newdata = pd.read_csv('Contino_102622/contino_ac_102622/Contino_result_table.tsv', sep='\t', header=0)
newdata2 = newdata[['Sample name', 'Classification']]
table = {}
for key in list(newdata2['Sample name'].unique()):
  table[key] = set()

for ind in newdata2.index:
  table[newdata2['Sample name'][ind]].add(newdata2['Classification'][ind])

headers = {}
#sample, ecDNA, BFB, CNC, Linear
df = pd.DataFrame({'Sample name': pd.Series(dtype='str'), 'ecDNA': pd.Series(dtype='int'), 'BFB': pd.Series(dtype='int'), 'Complex non-cyclic': pd.Series(dtype='int'), 'Linear amplification': pd.Series(dtype='int')})
cells_per_sample = []
for key in table:
  curr_row = [key]
  if 'ecDNA' in table[key]:
    curr_row.append('ECDNA')
  else:
    curr_row.append(None)
  if 'BFB' in table[key]:
    curr_row.append('BFB')
  else:
    curr_row.append(None)
  if 'Complex non-cyclic' in table[key]:
    curr_row.append('CNC')
  else:
    curr_row.append(None)
  if 'Linear amplification' in table[key]:
    curr_row.append('LIN')
  else:
    curr_row.append(None)
  df.loc[len(df.index)] = curr_row
  curr_row.remove(key)


df = df.sort_values(['ecDNA', 'BFB', 'Complex non-cyclic', 'Linear amplification'], ascending=[False, False, False, False])
df = df.reset_index(drop=True)
print(df)

df_rows = df[['ecDNA', 'BFB', 'Complex non-cyclic', 'Linear amplification']]
for ind in df_rows.index:
  cells_per_sample.append(df_rows.loc[ind,:].values.flatten().tolist())
print(cells_per_sample)
data = []

#  Sample name ecDNA     BFB Complex non-cyclic Linear amplification
# 1       ESO51   AMP  FUSION           MISSENSE              INFRAME
# 6        OE33   AMP  FUSION               None              INFRAME
# 2       FLO-1   AMP    None           MISSENSE              INFRAME
# 5     OACP4-C   AMP    None           MISSENSE              INFRAME
# 3   JH-EsoAd1   AMP    None           MISSENSE                 None
# 4     OACM5.1   AMP    None               None                 None
# 0       ESO26  None  FUSION           MISSENSE                 None
# 7     SK-GT-4  None    None           MISSENSE              INFRAME
for ind in df.index:
  for i in range(4):
    x =  {}
    x['sample'] = str(df["Sample name"][ind])
    if cells_per_sample[ind][i] != None:
      if cells_per_sample[ind][i] == 'ECDNA':
        x['gene'] = 'ecDNA'
      if cells_per_sample[ind][i] == 'BFB':
        x['gene'] = 'BFB'
      if cells_per_sample[ind][i] == 'CNC':
        x['gene'] = 'Complex non-cyclic'
      if cells_per_sample[ind][i] == 'LIN':
        x['gene'] = 'Linear amplification'
      x['alteration'] = cells_per_sample[ind][i]
      x['type'] = cells_per_sample[ind][i]
      data.append(x)

print(data)



app = dash.Dash(__name__)

app.layout = html.Div([
    Oncoprint(
        data = data,
        showoverview=False,
        backgroundcolor='rgb(255,255,255)',
        colorscale={'ECDNA': '#FF4343',
        'CNC' : '#FFBE00' , 
        'BFB' :'#00462E', 
        'LIN': '#1B6FB9'}

    ),
    html.Div(id='Custom-output')
])

@app.callback(
    Output('Custom-output', 'children'),
    Input('Custom', 'eventDatum')
)
def update_output(event_datum):
    if event_datum is None or len(event_datum) == 0:
        return 'There are no event data. Hover over or click on a part \
        of the graph to generate event data.'

    event_datum = json.loads(event_datum)

    return [
        html.Div('{}: {}'.format(
            key,
            str(event_datum[key]).replace('<br>', '\n')
        ))
        for key in event_datum.keys()]


# @app.callback(
#     dash.dependencies.Output('Custom-output', 'children'),
#     [dash.dependencies.Input('Custom', 'eventDatum')])
# def update_output_div(event_data):
#     return 'You have selected {}'.format(event_data)

if __name__ == '__main__':
    app.run_server(debug=True, port=8051)
