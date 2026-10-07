import React,{useEffect,useState} from "react";import {createRoot} from "react-dom/client";import "./style.css";
const Card=({t,v})=><div className="card"><span>{t}</span><b>{v}</b></div>;
function App(){
 const [d,setD]=useState({users:[],stats:{},logs:[]});const [q,setQ]=useState("");const [amt,setAmt]=useState({});
 const load=async()=>setD(await (await fetch("/api/data")).json());
 useEffect(()=>{load();const i=setInterval(load,8000);return()=>clearInterval(i)},[]);
 const adj=async id=>{if(!amt[id])return;await fetch("/api/adjust",{method:"POST",body:JSON.stringify({id,amt:amt[id]})});setAmt({...amt,[id]:""});load()};
 const s=d.stats,us=d.users.filter(u=>(u.name+u.phone+u.id).toLowerCase().includes(q.toLowerCase()));
 return <div className="wrap"><h1>🎱 Jo Bingo Admin</h1>
  <div className="stats"><Card t="Users" v={d.users.length}/><Card t="Online now" v={d.online}/><Card t="Game phase" v={d.phase}/>
   <Card t="Rounds" v={s.round}/><Card t="Deposits" v={s.deposit}/><Card t="Withdrawals" v={s.withdraw}/><Card t="House profit" v={s.house}/></div>
  <h2>Users</h2><input placeholder="Search name / phone / id" value={q} onChange={e=>setQ(e.target.value)}/>
  <div className="scroll"><table><thead><tr><th>ID</th><th>Name</th><th>Phone</th><th>Balance</th><th>Add / subtract birr</th></tr></thead><tbody>
   {us.map(u=><tr key={u.id}><td>{u.id}</td><td>{u.name}</td><td>{u.phone}</td><td>{u.bal}</td>
    <td><input type="number" className="n" value={amt[u.id]||""} onChange={e=>setAmt({...amt,[u.id]:e.target.value})}/> <button onClick={()=>adj(u.id)}>Apply</button></td></tr>)}
  </tbody></table></div>
  <h2>Recent activity</h2><div className="scroll"><table><thead><tr><th>Time</th><th>Type</th><th>User</th><th>Amount</th></tr></thead><tbody>
   {d.logs.map((l,i)=><tr key={i}><td>{new Date(l.ts*1000).toLocaleString()}</td><td>{l.kind}</td><td>{l.uid||"-"}</td><td>{l.amt}</td></tr>)}
  </tbody></table></div></div>}
createRoot(document.getElementById("root")).render(<App/>);
