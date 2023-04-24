import React, {Component} from 'react';
import {omit} from 'ramda';

import CustomOncoPrint from './Oncoprint';
import {custom_defaultProps, custom_propTypes} from './Oncoprint.react'

console.log('CustomOncoprint', CustomOncoPrint)
console.log('propTypes', custom_propTypes)
console.log('defaultProps', custom_defaultProps)


export default class OncoPrint extends Component {
    constructor(props) {
        super(props);
        this.handleChange = this.handleChange.bind(this);
    }

    // Bind to Dash event handler that puts event back into props
    handleChange(event) {
        const eventObj = JSON.stringify(event);
        this.props.setProps({eventDatum: eventObj});
    }

    


    render() {
        const {id, eventDatum, loading_state} = this.props;

        return (
            <div
                id={id}
                eventDatum={eventDatum}
                data-dash-is-loading={
                    (loading_state && loading_state.is_loading) || undefined
                }
            >
                <CustomOncoPrint
                    onChange={this.handleChange}
                    {...omit(['setProps', 'loading_state'], this.props)}
                />
            </div>
        );

    }
}



OncoPrint.propTypes = {
  /**
   * The ID of this component, used to identify dash components
   * in callbacks. The ID needs to be unique to the component.
   */
  id: PropTypes.string,

  /**
   * Dash-assigned callback that should be called whenever any of the
   * properties change.
   */
  setProps: PropTypes.func,

  /**
   * A Dash prop that returns data on clicking, hovering or resizing the viewer.
   */
  eventDatum: PropTypes.object,

  /**
   * Input data, in CBioPortal format where each list entry is a dict
   * consisting of 'sample', 'gene', 'alteration', and 'type'
   */
  data: PropTypes.array,

  // TODO: Add remove empty columns prop

  /**
   * Adjusts the padding (as a proportion of whitespace) between two tracks.
   * Value is a ratio between 0 and 1.
   * Defaults to 0.05 (i.e., 5 percent). If set to 0, plot will look like a heatmap.
   */
  padding: PropTypes.number,

  /**
   * If not null, will override the default OncoPrint colorscale.
   * Default OncoPrint colorscale same as CBioPortal implementation.
   * Make your own colrscale as a {'mutation': COLOR} dict.
   * Supported mutation keys are ['MISSENSE, 'INFRAME', 'FUSION',
   * 'AMP', 'GAIN', 'HETLOSS', 'HMODEL', 'UP', 'DOWN']
   * Note that this is NOT a standard plotly colorscale.
   */
  colorscale: PropTypes.oneOfType([PropTypes.bool, PropTypes.object]),

  /**
   * Default color for the tracks, in common name, hex, rgb or rgba format.
   * If left blank, will default to a light grey rgb(190, 190, 190).
   */
  backgroundcolor: PropTypes.string,

  /**
   *.Reset windowing to user preset on initial range or data change.
   */
  range: PropTypes.array,

  /**
   *.Toogles whether or not to show a legend on the right side of the plot,
   * with mutation information.
   */
  showlegend: PropTypes.bool,

  /**
   *.Toogles whether or not to show a heatmap overview of the tracks.
   */
  showoverview: PropTypes.bool,

  /**
   * Width of the OncoPrint.
   * Will disable auto-resizing of plots if set.
   */
  width: PropTypes.oneOfType([PropTypes.number, PropTypes.string]),

  /**
   * Height of the OncoPrint.
   * Will disable auto-resizing of plots if set.
   */
  height: PropTypes.oneOfType([PropTypes.number, PropTypes.string]),

  /**
   * Object that holds the loading state object coming from dash-renderer
   */
  loading_state: PropTypes.shape({
      /**
       * Determines if the component is loading or not
       */
      is_loading: PropTypes.bool,
      /**
       * Holds which property is loading
       */
      prop_name: PropTypes.string,
      /**
       * Holds the name of the component that is loading
       */
      component_name: PropTypes.string,
  }),
};

OncoPrint.defaultProps = {
  // Layout
  padding: 0.05,
  colorscale: null,
  backgroundcolor: 'rgb(190, 190, 190)',
  range: [null, null],
  showlegend: true,
  showoverview: true,
  width: null,
  height: 500,
};

export {OncoPrint as RealOncoPrint};