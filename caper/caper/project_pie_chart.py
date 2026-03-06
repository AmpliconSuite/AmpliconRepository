import pandas as pd
import plotly.graph_objects as go
import logging


def pie_chart(sample, fa_cmap):
    df = pd.DataFrame(sample)

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

    # NOW filter out None/NaN/empty AFTER we've counted everything
    class_counts_df = class_counts_df[
        class_counts_df['Classification'].notna() &
        (class_counts_df['Classification'] != '') &
        (class_counts_df['Classification'] != 'None')
        ]

    class_counts_df['Percentage'] = (class_counts_df['Sample_count'] / total_samples * 100).round(1)
    class_counts_df = class_counts_df.sort_values('Sample_count', ascending=True)

    # Debug output
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

    # Create horizontal bar chart
    fig = go.Figure()

    for _, row in class_counts_df.iterrows():
        # Place text inside if bar is >= 30%, outside otherwise
        text_position = 'inside' if row['Percentage'] >= 30 else 'outside'

        fig.add_trace(go.Bar(
            y=[row['Display_label']],
            x=[row['Percentage']],
            name=row['Classification'],
            orientation='h',
            marker_color=fa_cmap.get(row['Classification'], '#808080'),
            text=[f"{row['Percentage']}% ({row['Sample_count']})"],
            textposition=text_position,
            hovertemplate=f"<b>{row['Classification']}</b><br>" +
                          f"{row['Sample_count']} samples ({row['Percentage']}%)<extra></extra>"
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

    return fig.to_html(full_html=False, config=updated_config_dict, div_id='project_pie_plotly_div')