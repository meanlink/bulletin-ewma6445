/**
 * @typedef {{id:number,name:string,source:string,rows:number,checksum:string,revision:number,rejected_rows:number}} Dataset
 * @typedef {{current_rows:number,received_total:number,accepted_total:number,ignored_total:number,rejected_total:number,batches:number,balanced:boolean}} IntakeSummary
 * @typedef {{seq:number,source:string,received:number,accepted:number,ignored:number,rejected:number}} IngestBatch
 * @typedef {{batch_seq:number,index:number,reason:string,record:Object}} RejectedRecord
 * @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob
 * @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule
 */
export {};
