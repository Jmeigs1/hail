// An optimized version of express-jwt, should be more cpu-efficient,
// while also not having incompatibility with polka (or other frameworks)
// due to custom Error extensions
const jwtMiddleware = require.main.require('./common/auth/polka-jwt');
const fetch = require('isomorphic-unfetch');
const { promisify } = require('util');
const jwksRsa = require('jwks-rsa');

const InvalidTokenError = require.main.require(
  './common/auth/errors/InvalidTokenError'
);

const redisClient = require('redis').createClient({
  // parser: 'hiredis'
});

const getAsync = promisify(redisClient.get).bind(redisClient);
const setAsync = promisify(redisClient.set).bind(redisClient);

const tokenUrl = process.env.AUTH0_MANAGEMENT_API_TOKEN_URL;
const managementUrl = process.env.AUTH0_MANAGEMENT_API_URL;

const options = {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    grant_type: 'client_credentials',
    client_id: process.env.AUTH0_MANAGEMENT_API_CLIENT,
    client_secret: process.env.AUTH0_MANAGEMENT_API_SECRET,
    audience: process.env.AUTH0_MANAGEMENT_API_AUDIENCE
  })
};

const findProviderObject = (userManagementResponse, userID) => {
  // [providerName, id]
  const idParts = userID.split('|');

  const githubObject = userManagementResponse.identities.find(
    // interestingly exact comparison of strings matches,
    // clearly does not treat String(val) as an object of string class
    // since obj1 !== obj2 unless &obj1 === &obj2
    o => o.provider === idParts[0] && String(o.user_id) === idParts[1]
  );

  return githubObject;
};

// TODO: Prefetch all user access tokens in Redis
// .json() returns a promise;
// https://stackoverflow.com/questions/41111411/node-fetch-only-returning-promise-pending
const auth0oauthTokenPromise = fetch(tokenUrl, options).then(r => r.json());

auth0oauthTokenPromise
  .then(oauthRes =>
    fetch(managementUrl, {
      headers: {
        Authorization: `Bearer ${oauthRes.access_token}`
      }
    }).then(r => r.json())
  )
  .then(userData => {
    userData.forEach(user => {
      user.identities.forEach(idObject => {
        // TODO: Make sure closed over variables stay in scope
        const userID = `${idObject.provider}|${idObject.user_id}`;
        const accessToken = idObject.access_token;

        getAsync(userID).then(val => {
          if (!val) {
            setAsync(userID, accessToken);
          }
        });
      });
    });
  });

class AuthMiddleware {
  constructor(config) {
    const attachProperty = (config && config.attachProperty) || 'user';

    const secret = jwksRsa.expressJwtSecret({
      cache: true,
      rateLimit: true,
      jwksRequestsPerMinute: 5,
      jwksUri: process.env.AUTH0_WEB_KEY_SET_URL
    });

    // sub is what auth0 uses
    this.getUserId = user =>
      // sub is what auth0 uses
      user && (user.id || user._id || user.sub);

    const commonOpts = {
      userProperty: attachProperty,
      secret,
      // This isn't exactly right, the access token will contain very little
      // user-specific code, just the sub and thes scope array
      requestProperty: 'user',
      audience: process.env.AUTH0_AUDIENCE,
      issuer: process.env.AUTH0_DOMAIN,
      algorithms: ['RS256'],
      credentialsRequired: true
    };

    this.verifyTokenPermissiveFn = jwtMiddleware(
      Object.assign({}, commonOpts, {
        credentialsRequired: false
      })
    );

    this.verifyToken = jwtMiddleware(commonOpts);

    this.getAuth0ProviderAccessToken = this.getAuth0ProviderAccessToken.bind(
      this
    );
  }

  // like verify token, but will check if the user submitted a valid refresh token
  // this is useful in the case the user hasn't had a chance to refresh their
  // id_token, but still has a valid refresh token
  verifyTokenPermissive() {
    return (req, res, next) => this.verifyTokenPermissiveFn(req, res, next);
  }

  async getAuth0ProviderAccessToken(req, _, next) {
    try {
      const accessToken = await this.extractAccessToken(req.user);
      req.accessToken = accessToken;
      next();
    } catch (e) {
      next(e);
    }
  }

  async extractAccessToken(user) {
    // No need to catch, express handles it
    // https://expressjs.com/en/guide/error-handling.html
    const userID = this.getUserId(user);

    if (!userID) {
      throw new InvalidTokenError();
    }

    // NOTE: This requires userID to change by auth0provide
    // typically auth0, at least social, connections
    // are in the form provider|id
    let accessToken = await getAsync(userID);

    if (accessToken) {
      return accessToken;
    }

    const oauthRes = await auth0oauthTokenPromise;

    //Ensure we acutally resolve the promise with json data before handing
    //to synchronous methods
    const userManagementResponse = await fetch(`${managementUrl}/${userID}`, {
      headers: {
        Authorization: `Bearer ${oauthRes.access_token}`
      }
    }).then(r => r.json());

    const githubData = findProviderObject(userManagementResponse, userID);

    accessToken = githubData.access_token;

    setAsync(userID, accessToken);
    return accessToken;
  }
}

module.exports = AuthMiddleware;
