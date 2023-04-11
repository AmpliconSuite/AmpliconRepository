import custom_oncoprint
from dash import Dash, callback, html, Input, Output

app = Dash(__name__)

app.layout = html.Div([
    custom_oncoprint.CustomOncoprint(
        id='input',
        value='my-value',
        label='my-label'
    ),
    html.Div(id='output')
])


@callback(Output('output', 'children'), Input('input', 'value'))
def display_output(value):
    return 'You have entered {}'.format(value)


if __name__ == '__main__':
    app.run_server(debug=True)
