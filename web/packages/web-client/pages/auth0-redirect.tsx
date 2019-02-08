import { PureComponent } from 'react';
import { authenticationCallback } from '../libs/auth';
import Router from 'next/router';
import jscookies from 'js-cookie';
import { isServer } from '../libs/utils';

class Redirect extends PureComponent {
  state = {
    unauthorized: false
  };

  constructor(props: any) {
    super(props);

    if (!isServer) {
      authenticationCallback(err => {
        if (err) {
          console.error(err);
          this.state.unauthorized = true;
          return;
        }

        const referrer = jscookies.get('referrer');

        if (referrer) {
          jscookies.remove('referrer');
        }

        Router.replace(referrer || '/');
      });
    }
  }

  render() {
    return (
      <div className="centered">
        {this.state.unauthorized ? 'Unauthorized' : 'Securing stuff...'}
      </div>
    );
  }
}

export default Redirect;
