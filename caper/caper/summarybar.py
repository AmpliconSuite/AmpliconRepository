import pandas as pd
import plotly.graph_objects as go
import logging


# Legacy classification names and their canonical equivalents, so that
# mixed-vintage projects are counted correctly rather than split into two bars.
# The project page's sample filter applies the same mapping (sample_table.py):
# a click on a bar must find the samples that bar counted.
LEGACY_CLASSIFICATION_NAMES = {
    'Linear amplification': 'Linear',
    'Complex non-cyclic': 'Complex-non-cyclic',
}


def canonical_classification(name):
    return LEGACY_CLASSIFICATION_NAMES.get(name, name)


def summarybar(sample, fa_cmap):
    df = pd.DataFrame(sample)

    df['Classification'] = df['Classification'].replace(LEGACY_CLASSIFICATION_NAMES)

    # Get unique samples and their classifications
    sample_classifications = df.groupby('Sample_name')['Classification'].apply(lambda x: set(x)).reset_index()

    # Count how many samples have each classification type
    classification_counts = {}
    total_samples = len(sample_classifications)  # This stays correct - all samples

    for classifications in sample_classifications['Classification']:
        for classification in classifications:
            # Count ALL classifications including None/NaN for now
            if classification not in classification_counts:
                classification_counts[classification] = 0
            classification_counts[classification] += 1

    # Convert to DataFrame
    class_counts_df = pd.DataFrame(list(classification_counts.items()),
                                   columns=['Classification', 'Sample_count'])

    # Filter out None/NaN/empty/NA AFTER we've counted everything
    class_counts_df = class_counts_df[
        class_counts_df['Classification'].notna() &
        (class_counts_df['Classification'] != '') &
        (class_counts_df['Classification'] != 'None') &
        (class_counts_df['Classification'] != 'NA')
        ]

    class_counts_df['Percentage'] = (class_counts_df['Sample_count'] / total_samples * 100).round(1)
    class_counts_df = class_counts_df.sort_values('Sample_count', ascending=True)

    logging.info(f"Sample classification percentages (n={total_samples}):")
    for _, row in class_counts_df.iterrows():
        logging.info(f"  {row['Classification']}: {row['Percentage']}% ({row['Sample_count']} samples)")

    # Abbreviate labels
    def abbreviate_label(label):
        if label in ['Complex non-cyclic', 'Complex-non-cyclic']:
            return 'CNC'
        elif label == 'Linear amplification':
            return 'Linear'
        return label

    class_counts_df['Display_label'] = class_counts_df['Classification'].apply(
        lambda x: str(abbreviate_label(x)) + '  ')

    fig = go.Figure()

    for _, row in class_counts_df.iterrows():
        text_position = 'inside' if row['Percentage'] >= 30 else 'outside'

        fig.add_trace(go.Bar(
            y=[row['Display_label']],
            x=[row['Percentage']],
            name=row['Classification'],
            orientation='h',
            marker_color=fa_cmap.get(row['Classification'], '#808080'),
            text=[f"{row['Percentage']}% ({row['Sample_count']})"],
            textposition=text_position,
            # The project page filters its sample table on a click; see
            # project.html. Saying so here is the only place a visitor learns it.
            hovertemplate=f"<b>{row['Classification']}</b><br>" +
                          f"{row['Sample_count']} samples ({row['Percentage']}%)<br>" +
                          "<i>Click to list these samples</i><extra></extra>"
        ))

    fig.update_layout(
        showlegend=False,
        title=dict(
            text="Percentage of samples containing classification",
            x=0.5,
            y=0.9,
            xanchor='center',
            yanchor='middle',
            font=dict(size=12)
        ),
        width=400,
        height=400,
        margin={'t': 55, 'b': 5, 'r': 20, 'l': 20},
        font=dict(size=12),
        plot_bgcolor='white'
    )

    fig.update_xaxes(
        gridcolor='white',
        rangemode='tozero',
        ticks='outside',
        range=[0, 100],
        title=None
    )
    fig.update_yaxes(
        gridcolor='white',
        title=None
    )

    updated_config_dict = {
        'displayModeBar': True,
        'toImageButtonOptions': {'format': 'svg'}
    }

    # include_plotlyjs=False -- see the note in sample_plot.py.
    return fig.to_html(full_html=False, config=updated_config_dict, div_id='summarybar_plotly_div',
                       include_plotlyjs=False)
