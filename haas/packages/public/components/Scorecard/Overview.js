// TODO: Convert to TypeScript, add prop validation
import { Component } from 'react';
import Link from 'next/link';

import '../../styles/scorecard/scorecard.scss';

const PullRequestLink = props =>
  props.data.map((pr, idx) => [
    idx > 0 && ', ',
    <Link href={`${pr.html_url}`} key={idx}>
      <a target="_b"> {pr.id}</a>
    </Link>
  ]);

const Overview = ({ data }) => {
  if (data === null) {
    return <div>No data</div>;
  }

  const { user_data, unassigned, urgent_issues } = data;

  return (
    <div id="scorecard" className="grid-container">
      {urgent_issues.length > 0 && (
        <div className="grid-item" style={{ marginBottom: 16 }}>
          <h3 id="urgent">Urgent</h3>

          <table>
            <thead>
              <tr>
                <th align="left">
                  <h5>Asignee</h5>
                </th>
                <th align="left">
                  <h5>Time outstanding</h5>
                </th>
                <th align="left">
                  <h5>Issue</h5>
                </th>
              </tr>
            </thead>
            <tbody>
              {urgent_issues.map((issue, idx) => (
                <tr key={idx}>
                  <td align="left">
                    <a href={`/users/${issue.USER}`}>{issue.USER}</a>
                  </td>
                  <td align="left">{issue.AGE}</td>
                  <td align="left">
                    <a href={issue.ISSUE.html_url}>{issue.ISSUE.title}</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="grid-item">
        <h3>Nominal</h3>
        <table>
          <thead>
            <tr>
              <th align="left">
                <h5>User</h5>
              </th>
              <th align="left">
                <h5>Review</h5>
              </th>
              <th align="left">
                <h5>Changes</h5>
              </th>
              <th align="left">
                <h5>Issues</h5>
              </th>
            </tr>
          </thead>
          <tbody>
            {Object.keys(user_data).map((userName, idx) => (
              <tr key={idx}>
                <td align="left">
                  <Link href={`/users/${userName}`}>
                    <a target="_b"> {userName}</a>
                  </Link>
                </td>
                <td align="left" className="link">
                  <PullRequestLink data={user_data[userName].NEEDS_REVIEW} />
                </td>

                <td align="left" className="link">
                  <PullRequestLink
                    data={user_data[userName].CHANGES_REQUESTED}
                  />
                </td>
                <td align="left" className="link">
                  <PullRequestLink data={user_data[userName].ISSUES} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ marginLeft: 10, marginBottom: 16 }} className="link">
        Unassigned: <PullRequestLink data={unassigned} />
      </div>
    </div>
  );
};

export default Overview;
