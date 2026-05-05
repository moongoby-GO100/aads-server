<?php if (!defined('BASEPATH')) exit('No direct script access allowed');

class Products extends CI_Controller
{
    /**
     * Variable for loading the config array into
     * @var array
     */
    public $info;
    public $view_data;
    public $tmp_upload_url;

	function __construct()
	{
		parent::__construct();
		//debug($this->config);exit;
		//$this->output->enable_profiler(TRUE);

if(!$this->session->userdata('user_id')) {
		if($this->input->is_ajax_request()) {
			http_response_code(401);
			echo json_encode(['error' => 'login_required', 'message' => '세션이 만료되었습니다. 다시 로그인해주세요.']);
			exit;
		}
		redirect('/auth/login/');
	}

        $this->config_vars = $this->config->item('market');
		//debug($this->session->all_userdata());

		//$this->load->library('esmplus_scrap');
		//Console::log($this->esmplus_scrap->config_vars);

		$this->load->library('esmplus_scrap');
		$this->load->library('elevenSt_scrap');
		$this->load->library('coupang_scrap');
		$this->load->library('storefarm_scrap');
		//$this->load->library('wemakeprice_scrap');
		$this->load->library('sinsang_scrap');

        // 알리고 lib 추가(2021.10.22)
        $this->load->library('aligo_sms');

		$this->view_data = $this->session->all_userdata();

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
			$this->tmp_upload_url = $this->config->item('user_temp_image_dir').$user_id;

			// if(!$this->view_data['username']) alert('상품관리자 정보에 사용자명이 없으면 이용할 수 없습니다!');
		}
		else
			$this->tmp_upload_url = $this->config->item('user_temp_image_dir').$this->session->userdata('user_id');

		if(!is_dir($this->tmp_upload_url)) mkdir($this->tmp_upload_url, 0755, TRUE);
		//debug($this->tmp_upload_url);exit;

		$this->info['today'] = date('Y-m-d', time());
		$this->info['created'] = date('Y-m-d H:i:s', time());
        $this->info['modified'] = '2017-11-01 00:00:00'; // 이미지 경로 뉴톡꺼 반영기준 타임
		//debug($this->info['created']);
		// debug($this->view_data);

		// 상품코드 이미지 다운 권한 세션
		//$this->view_data['down_level'] = $this->session->userdata('down_level');
			$this->view_data['REMOTE_ADDR'] = $_SERVER["REMOTE_ADDR"];

			}

	function _goodscode_image_files($goods_code)
	{
		$img_dir = $this->config->item('user_goodscode_img_dir') . $goods_code . '/';
		$files = glob($img_dir . '*');
		$names = array();
		if ($files) {
			foreach ($files as $f) {
				if (is_file($f)) $names[] = basename($f);
			}
		}
		natsort($names);
		return array_values($names);
	}

		function _download_unique_zip_filename($prefix)
		{
			$user_id = $this->session->userdata('user_id') ? $this->session->userdata('user_id') : '0';
			$token = substr(md5(uniqid('', TRUE).mt_rand()), 0, 8);
			return $prefix.'_'.$user_id.'_'.date('YmdHis').'_'.$token.'.zip';
		}

		function _download_request_zip_filename($prefix, $default_file)
		{
			$file = $this->input->get('file');
			if(!$file) return $default_file;

			$file = basename($file);
			if(substr($file, -4) !== '.zip' || strpos($file, $prefix.'_') !== 0)
				alert_close('다운로드 파일이 유효하지 않습니다!');

			return $file;
		}

		function index()
		{
			$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		$this->view_data['link_tag3'] = link_tag('/include/plugins/datepicker/datepicker3.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/extensions/Scroller/css/dataTables.scroller.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/examples/resources/syntax/shCore.css');

		// 카카오스토리 APP Key 추가
		$query = $this->db->query("SELECT sns_key FROM user_sns WHERE user_id='{$this->view_data['user_id']}' AND sns_gb='A' AND activated='1'");
		$row = $query->row();
		$sns_key = $row->sns_key;
		if($sns_key)
			$this->view_data['sns_key'] = $sns_key;
		else
			$this->view_data['sns_key'] = 'eadf8be73fdee750e41725566405edbf'; // 디폴트 키(APP명 : 쇼핑몰)

		// 카테고리 목록
		$this->view_data['cate1'] = $this->get_category();
		//debug($this->config_vars);
		//debug($this->view_data);

		// FTP 관리 목록
		$query = $this->db->query("SELECT id, ftp_name FROM store_ftp_config WHERE 1");
		$this->view_data['ftplist'] = $query->result();
        // debug($this->view_data['ftplist']);

        $this->view_data['ip'] = $_SERVER["REMOTE_ADDR"];

		$this->load->view('top', $this->view_data);

//		if($_SERVER["REMOTE_ADDR"] == "121.134.129.200"){
//			$this->load->view('products/goods_list_20230830.php', $this->view_data);
//		}else{
			if($_SERVER["REMOTE_ADDR"] == "218.157.131.10")
				$this->load->view('products/goods_list.php', $this->view_data);
			else
				$this->load->view('products/goods_list.php', $this->view_data);
//		}	
//		$this->load->view('bottom');
	}

	// 상품 조회
	function goods_master_ajax_list()
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->helper('directory');

		// thumbnail 생성
		$this->load->library('image_lib');

		//debug($_REQUEST);
		$user_id = $this->session->userdata('user_id');

		$this->load->model('goods_m');
		$etc = $this->goods_m->get_goods_option_etc();

		$where = "";

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$cate1 = $this->input->post('cate1');
		$cate2 = $this->input->post('cate2');
		$cate3 = $this->input->post('cate3');

		$sCreated = $this->input->post('sCreated');
		$eCreated = $this->input->post('eCreated');

		$sGoodsOnly = $this->input->post('sGoodsOnly');
		$sGoodsOnlyDay = $this->input->post('sGoodsOnlyDay');

		$sGoodsEtc9SearchVal = $this->input->post('sGoodsEtc9SearchVal');
		$eGoodsEtc9SearchVal = $this->input->post('eGoodsEtc9SearchVal');

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;

				if($fieldName == 'GoodsName') $fieldData = strtoupper($fieldData);
				if($fieldName == 'GoodsCode') $fieldData = strtolower($fieldData);
                if($fieldName == 'created') $fieldName = 'GS.created';
                if($fieldName == 'modified') $fieldName = 'GS.modified';
                if($fieldName == 'activated') $fieldName = 'GS.activated';

                if($fieldName == 'ddg_send_yn' && $fieldData != 'NV') $fieldName = 'GS.ddg_send_yn';
                if($fieldName == 'ddg_send_yn' && $fieldData == 'NV'){$fieldName = 'GS.activated'; $fieldData = 'N';}

                if($fieldName == 'GS.ddg_send_yn' && $fieldData == 'N'){
                    $fieldName = " GS.activated = 'Y' AND ".$fieldName;
                }

				switch ($rule->op)
				{
					case "eq":
					    $fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "")
				{
					if($fieldName == 'GoodsName')
					{
						$whereArray[] = "(".$fieldName.$fieldOperation." OR DanharooGoodsName".$fieldOperation.")";
					}
                    // 상품코드 ',' 구분으로 여러 상품 검색 변경(2022.02.14)
					else if($fieldName == 'GoodsCode')
					{
						$whereArray[] = "( GoodsCode IN ('".implode('\',\'', explode(',', $fieldData))."') )";
					}
					else
						$whereArray[] = $fieldName.$fieldOperation;
				}
				else {
					if($fieldName == 'filecnt' && $fieldData != '')
					{
						if($fieldData == 'Y') $whereArray[] = 'GSC.filecnt > 0';
						if($fieldData == 'N') $whereArray[] = 'GSC.filecnt = 0';
						// if($fieldData == 'X') $whereArray[] = 'GSC.filecnt = null';
						//debug($fieldName);
					}
				}
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = "";
			// debug($where);
		}

		if($cate1)
		{
			$where .= " and Category1='$cate1' ";

			if($cate2)
			{
				$where .= " and Category2='$cate2' ";

				if($cate3)
					$where .= " and Category3='$cate3' ";
			}
		}

		if($sCreated && !$eCreated)
			$where .= " and GS.created >= '$sCreated' ";
		if($sCreated && $eCreated)
			$where .= " and GS.created BETWEEN '$sCreated 00:00:00' AND '$eCreated 23:59:59' ";
			//debug($where);

		// (2020.08.26) 업무진행상태 변경일시검색 추가
		if($sBizProgressUpdateSearchVal && !$eBizProgressUpdateSearchVal)
			$where .= " and BizProgressUpdate >= '$eBizProgressUpdateSearchVal' ";
		if($sBizProgressUpdateSearchVal && $eBizProgressUpdateSearchVal)
			$where .= " and BizProgressUpdate BETWEEN '$sBizProgressUpdateSearchVal 00:00:00' AND '$eBizProgressUpdateSearchVal 23:59:59' ";
			//debug($where);

        // 원가 범위 검색 반영(2022.02.15)
        // 시작 금액만 있을 때
        if($sGoodsEtc9SearchVal && !$eGoodsEtc9SearchVal)
			$where .= " and GS.GoodsEtc9 >= ".$sGoodsEtc9SearchVal;
        // 종료 금액만 있을 때
        if(!$sGoodsEtc9SearchVal && $eGoodsEtc9SearchVal)
			$where .= " and GS.GoodsEtc9 <= ".$eGoodsEtc9SearchVal;
        // 시작 금액, 종료 금액 있을 때
        if($sGoodsEtc9SearchVal && $eGoodsEtc9SearchVal)
			$where .= " and GS.GoodsEtc9 BETWEEN ".$sGoodsEtc9SearchVal." AND ".$eGoodsEtc9SearchVal;

		// (2020.05.26) 추가
		if($sGoodsOnly) {
			$where .= " and GoodsOnly = '$sGoodsOnly' and GoodsOnlyDay != '0000-00-00' ";
		}

		// (2020.05.26) 추가
		if($sGoodsOnlyDay)
			$where .= " and GoodsOnlyDay = '$sGoodsOnlyDay' ";

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스

			// 2020.06.25 수정 - 입력된 브랜드 상품만 보이게 반영
			if($this->view_data['brandAll'] == 'N')
			{
				$brandEtcArr = explode(',', $this->view_data['brandEtc']);
				$where .= " and BrandName in ('".implode("','", $brandEtcArr)."')";
				// debug($where);
			}
		}
	    // debug($where);

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "GS.created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 상품목록
		// $sql = "SELECT count(id) AS cnt FROM goods AS GS WHERE user_id='{$user_id}' {$where}";
        // debug($sql);
		$query = $this->db->query("SELECT
										count(GS.id) AS cnt
										FROM
											goods 		    AS GS LEFT OUTER JOIN
											goods_code	    As GSC ON GS.GoodsCode = GSC.gcode LEFT OUTER JOIN
											goods_detail    As GSD ON GS.id = GSD.goods_id
										WHERE
											user_id='{$user_id}' {$where}
								");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$responce = new stdClass();

		$sql = "SELECT
					GS.*,
					GSI.img0, GSI.img1,
					GSC.filecnt,
					GSD.CoordiGoodsCodes
					FROM
						goods		    AS GS LEFT OUTER JOIN
						goods_image	    As GSI ON GS.id = GSI.goods_id LEFT OUTER JOIN
						goods_code	    As GSC ON GS.GoodsCode = GSC.gcode LEFT OUTER JOIN
						goods_detail    As GSD ON GS.id = GSD.goods_id
					WHERE
						user_id='{$user_id}' {$where}
					ORDER BY
						$sidx $sord
					LIMIT
						$start , $limit
		";
        // debug($sql);
		//$query = $this->db->query("SELECT id, user_id, GoodsName, GoodsPrice, GoodsCount, GoodsImage, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				$img_home = $this->config->config['user_temp_image_dir'].$row->user_id;	// 절대경로
				$img_path = '/data/files/goods/'.$row->user_id;								// 페이지경로
				//debug($img_home);

				//debug(count(explode('/', $row->GoodsImage)));
				if(count(explode('/', $row->GoodsImage)) > 1)
					$GoodsImage	= $row->GoodsImage;
				else
				{
					if($row->GoodsImage)
					{
						$GoodsImage	 = '/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->GoodsImage;
						//$GoodsImage1 = ($row->img1)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img1:'';
						//$GoodsImage2 = ($row->img2)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img2:'';
						//$GoodsImage3 = ($row->img3)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img3:'';
						//$GoodsImage4 = ($row->img4)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img4:'';
					}
					else $GoodsImage = '';

					for($j=0; $j<2; $j++)
					{
						$img_name = $row->{'img'.$j};
						if($img_name)
						{
							$config['source_image']	= $img_home.'/'.$img_name;
							$config['new_image']	= $img_home.'/thumbnail/';
							if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

							if(!is_file($config['new_image'].$img_name))
							{
								$config['image_library'] = 'gd2';
								$config['maintain_ratio'] = TRUE;
								$config['width']	= 100;
								$config['height']	= 100;
								//debug($config);
								$this->image_lib->initialize($config);
								$this->image_lib->resize();
							}
						}

						${'GoodsImage'.$j} = (is_file($config['new_image'].$img_name)) ? $img_path.'/thumbnail/'.$img_name : '';
					}
				}
				// debug($row);

				//if($row->SendCheck == 'Y') $SendCheck = '';

				// 상품코드별 이미지 파일수 디비 처리로 인해 주석처리(2021.03.03)
				// if($row->GoodsCode)
				// {
				// 	$goods_img_dir = $this->config->item('user_goodscode_img_dir').$row->GoodsCode.'/';
				// 	$map = directory_map($goods_img_dir);
				// 	$img_cnt = count($map['thumbnail']);
				// }
				// if(!$img_cnt) $img_cnt = 0;

				$userid = '';
				if($row->GoodsEtc6)
				{
					/**
					 * 2025.1.24. 오병용 대표님 요청
					 * 동일한 아이디의 도매와 소매가 있을 경우 상품목록의 공급처상세페이지 링크 오류가 발생하여 도매로만 한정
					 **/
					//$query_a = $this->db->query("SELECT userid FROM users WHERE username='{$row->GoodsEtc6}' AND (auth_code = '4' OR auth_code = '3')");
					$query_a = $this->db->query("SELECT userid FROM users WHERE username='{$row->GoodsEtc6}' AND auth_code = '4'");
					$row_a = $query_a->row();
					$userid = $row_a->userid;
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													$row->GdsMstId,
													'',
													'',
													$row->created,
													$row->BizProgress,
													implode('<br />', explode(' ', $row->BizProgressUpdate)),
													$row->BrandName,
													$row->GoodsOnly,
													$row->activated,
													$row->mall_activated,
													$etc['status'][$row->GoodsEtc52],
													$etc['type'][$row->GoodsEtc48],
													$row->filecnt,
													$GoodsImage,
													$row->GoodsName,
													$row->GoodsCode,
													implode('<br />', explode(',', $row->CoordiGoodsCodes)), // 2021.08.24
													$row->GoodsEtc5,
													$row->GoodsEtc6,
													$row->GoodsEtc9,
													$row->GoodsEtc10,
													$row->GoodsEtc33,
													$row->GoodsEtc34,
													$row->GoodsEtc35,
													$row->GoodsPrice,
													$GoodsImage1,
													$row->re_created,
                                                    $row->modified,
                                                    $row->activated_day,
                                                    $row->GoodsOnlyDay,
													$row->DanharooGoodsName,
													$userid,
                                                    $row->ddg_send_yn,
													// $img_cnt,
												);
				$i++;
			}
		}
		// debug($responce);
		// debug($this->db->last_query());

		echo json_encode($responce);
	}

	// 상품 조회
	function goods_ajax_list()
	{
		//debug($_GET);
		$GdsMstId = $this->uri->segment(3);
		$user_id = $this->session->userdata('user_id');

		$query = $this->db->query("SELECT id, user_id, GdsMstId, market, GoodsName, GoodsPrice, GoodsCount, GoodsImage, GoodsNo, activated, created FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$GdsMstId}' ORDER BY id ASC");

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				// 11번가는 상품 이미지 조회 후 저장
				if($row->market == 'C')
				{
					$pos = strpos($row->GoodsImage, 'image.11st.co.kr');
					// 업데이트 된 이미지가 아닐 때 조회 후 저장
					if($pos === false && $row->GoodsNo)
					{
						$rtn_img = $this->elevenst_scrap->goods_img_process($row->id, $row->GoodsNo); // 상품번호로 조회
						if($rtn_img) $row->GoodsImage = $rtn_img;
					}
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													'',
													$row->GoodsNo,
													$row->GoodsImage,
													$this->config_vars['open_market2'][$row->market],
													$row->GoodsName,
													$row->GoodsPrice,
													$row->GoodsCount,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 상품 조회
	function ajax_list()
	{
		//debug($_GET);
		$user_id = $this->session->userdata('user_id');

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 상품목록
		$query = $this->db->query("SELECT count(id) AS cnt FROM goods WHERE user_id='{$user_id}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$query = $this->db->query("SELECT id, user_id, GdsMstId, market, GoodsName, GoodsPrice, GoodsCount, GoodsImage, GoodsNo, activated, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				// 11번가는 상품 이미지 조회 후 저장
				if($row->market == 'C')
				{
					$pos = strpos($row->GoodsImage, 'image.11st.co.kr');
					// 업데이트 된 이미지가 아닐 때 조회 후 저장
					if($pos === false && $row->GoodsNo)
					{
						$rtn_img = $this->elevenst_scrap->goods_img_process($row->id, $row->GoodsNo); // 상품번호로 조회
						if($rtn_img) $row->GoodsImage = $rtn_img;
					}
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->GoodsName,
													'MK-'.$row->GdsMstId.'#GK-'.$row->id,
													$row->GoodsName,
													'',
													$row->GoodsNo,
													$row->GoodsImage,
													$this->config_vars['open_market2'][$row->market],
													$row->GoodsPrice,
													$row->GoodsCount,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 전체 업무진행상태 조회
	function goods_biz_ajax_status()
	{
		$sql1 = "	SELECT
						COUNT(*) AS cnt, BizProgress AS code
					FROM
						goods
					WHERE
						BizProgress != ''
					GROUP BY BizProgress
					ORDER BY
						BizProgress ASC
		";
		$query1 = $this->db->query($sql1);
		$row1 = $query1->result();
		// debug(json_encode($row1, JSON_UNESCAPED_UNICODE));

		echo '{"info":{"success":true,"data1":'.json_encode($row1, JSON_UNESCAPED_UNICODE).'}}';
	}

	// 상품 업무진행상태 로그조회
	function goods_biz_ajax_html()
	{
		$GoodsId = $_REQUEST['GoodsId'];
		if(!$GoodsId || $GoodsId < 0) {
			echo '{"info":{"success":false}}';
		}

		$sql1 = "	SELECT
						GSBL.*,
						GS.GoodsName, GS.GoodsCode, GS.BrandName,
						US.userid, US.username
					FROM
						goods_biz_logs	AS GSBL LEFT OUTER JOIN
						goods			AS GS 	ON GSBL.goods_id = GS.id LEFT OUTER JOIN
						users			As US	ON GSBL.user_id = US.id
					WHERE
						GSBL.goods_id = {$GoodsId}
					ORDER BY
						GSBL.id DESC
		";
		$query1 = $this->db->query($sql1);
		$row1 = $query1->result();
		// debug(json_encode($row1, JSON_UNESCAPED_UNICODE));

		echo '{"info":{"success":true,"data1":'.json_encode($row1, JSON_UNESCAPED_UNICODE).'}}';
	}

	// 상품관리 로그조회
	function goods_action_ajax_html()
	{
		$GoodsId = $_REQUEST['GoodsId'];
		if(!$GoodsId || $GoodsId < 0) {
			echo '{"info":{"success":false}}';
		}

		$sql1 = "SELECT
					GSCL.*,
					GS.GoodsName, GS.GoodsCode, GS.BrandName,
					US.userid, US.username
					FROM
						goods_action_logs	AS GSCL LEFT OUTER JOIN
						goods				AS GS 	ON GSCL.goods_id = GS.id LEFT OUTER JOIN
						users				As US	ON GSCL.user_id = US.id
					WHERE
						GSCL.goods_id = {$GoodsId}
					ORDER BY
						GSCL.id DESC
		";
		$query1 = $this->db->query($sql1);
		$row1 = $query1->result();
		// debug(json_encode($row1, JSON_UNESCAPED_UNICODE));

		// $sql2 = "SELECT
		// 			GSCL.*,
		// 			GS.GoodsName, GS.GoodsCode, GS.BrandName,
		// 			US.userid, US.username
		// 			FROM
		// 				goods_action_logs	AS GSCL LEFT OUTER JOIN
		// 				goods				AS GS 	ON GSCL.goods_id = GS.id LEFT OUTER JOIN
		// 				users				As US	ON GSCL.user_id = US.id
		// 			WHERE
		// 				GSCL.goods_id = {$GoodsId} AND (GSCL.gb = 'B' OR GSCL.gb = 'C' OR GSCL.gb = 'E')
		// 			ORDER BY
		// 				GSCL.id DESC
		// ";
		// // debug($sql2);
		// $query2 = $this->db->query($sql2);
		// $row2 = $query2->result();
		// debug($row2);
		// debug(json_encode($row2, JSON_UNESCAPED_UNICODE));

		// echo '{"info":{"success":true,"data1":'.json_encode($row1, JSON_UNESCAPED_UNICODE).',"data2":'.json_encode($row2, JSON_UNESCAPED_UNICODE).'}}';
		echo '{"info":{"success":true,"data1":'.json_encode($row1, JSON_UNESCAPED_UNICODE).'}}';
	}

	/**
	 * STEP2-v1.1 상품명 자동화: 단하루 상품명 → 자사몰/쿠팡 미리보기 (AJAX, INSERT 없음)
	 * POST: danharooName, goodsCode, goods_code_6(optional)
	 * JSON: { success, goodsName, coupangName }
	 */
	function ajax_get_auto_names()
	{
		$this->output->set_content_type('application/json');
		$dan = $this->input->post('danharooName');
		$code = $this->input->post('goodsCode');
		$code6 = $this->input->post('goods_code_6');
		if (!is_string($dan)) $dan = '';
		if (!is_string($code)) $code = '';
		$this->load->helper('hangul_shift');
		$out = get_newtalk_auto_names($dan, $code, $this->db, $code6);
		$this->output->set_output(json_encode([
			'success' => true,
			'goodsName' => $out['goodsName'],
			'coupangName' => $out['coupangName']
		]));
	}

	// 상품수정폼
	function editing()
	{
		if(@$this->uri->segment(3))
		{
			$goods_id = $this->uri->segment(3);
			// debug($goods_id);
			$user_id = $this->session->userdata('user_id');

			$this->view_data['goods_array'] = $this->config->item('goods_array');
			// 상품 사이즈 목록, 2022.08.31
			$this->load->model('goods_m');
			$this->view_data['goods_size_cate'] = $this->goods_m->get_goods_size_value();
			$this->view_data['goods_model_list'] = $this->goods_m->get_goods_model_info();
			$this->view_data['goods_color_list'] = $this->goods_m->get_goods_color_list();
			$this->view_data['goods_fit_list'] = $this->goods_m->get_goods_fit_list();
			$this->view_data['etc'] = $this->goods_m->get_goods_option_etc();

			if(isset($goods_id))
			{
				// 상품관리자 추가(2020.06.06)
				if($this->view_data['auth_code'] == 12)
				{
					$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
				}

				//echo $_SERVER["REMOTE_ADDR"];
				// 등록 상품 정보 확인
				$sql = "SELECT
							GS.*,
							GSD.*,
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14, GSI.img15, GSI.img16, GSI.img17, GSI.img18, GSI.img19, GSI.img_etc0, GSI.img_etc1, GSI.img_etc2, GSI.img_etc3, GSI.img_etc4, GSI.img_etc5, GSI.img_etc6, GSI.img_etc7, GSI.img_etc8, GSI.img_etc9
							FROM
								goods AS		GS LEFT OUTER JOIN
								goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
								goods_image		As GSI ON GS.id = GSI.goods_id
							WHERE GS.id='{$goods_id}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();
				//debug($row);

				// 회원 상품이 맞는지 확인
				if($row->user_id == $user_id)
				{
					$this->view_data['Goods'] = $row;
					// debug($this->view_data['Goods']);
					$this->view_data['MarketName'] = $this->config_vars['open_market2'][$row->market];

					//debug($row->Category1);
					$this->view_data['cate1'] = $this->get_category('', false); // json_decode()
					// debug($this->view_data['cate1']);

					//debug($row->Category2);
					if($row->Category2)
					{
						$this->view_data['cate2'] = json_decode($this->get_category($row->Category2, false)); // json_decode()
						// debug($this->view_data['cate2']);

						//debug($row->Category3);
						$Category2_arr = explode('|', $row->Category2);
						$this->view_data['cate3'] = json_decode($this->get_category($Category2_arr[0].'|2', false)); // json_decode()
						// debug($this->view_data['cate3']);
					}
					else
						$this->view_data['cate2'] = json_decode($this->get_category($row->Category1, false)); // json_decode()
						//debug($this->view_data['cate2']);

					$this->view_data['Goods']->GoodsEtcUnserializes = [];
					if($this->view_data['Goods']->GoodsEtcSerializes) {
						$this->view_data['Goods']->OptionSizeArr = explode(',', $this->view_data['Goods']->OptionSize);
						$this->view_data['Goods']->GoodsEtcUnserializes = unserialize($this->view_data['Goods']->GoodsEtcSerializes);
						
						$this->load->model('goods_m');
						$this->view_data['sizeValue'] = $this->goods_m->get_goods_size_graph_value($this->view_data['Goods']->GoodsEtcUnserializes['OptionSizeKey']);
						// debug($this->view_data['Goods']->OptionSizeArr);
						// debug($this->view_data['Goods']->GoodsEtcUnserializes);
					}
				}
				else
					alert('회원님의 상품이 아닙니다!', '/products/index');
			}

			$this->load->helper('html');
			$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
			$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
			$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

			//debug($this->config_vars);
			// debug($this->view_data);

			$this->load->view('top_w', $this->view_data);
			$this->load->view('products/edit_form.php', $this->view_data);
			$this->load->view('bottom');
		}
		else
			alert('정상적인 접근이 아닙니다!', '/products/index');
	}

	// 상품수정폼 테스트
	function editing_test()
	{
		if(@$this->uri->segment(3))
		{
			$goods_id = $this->uri->segment(3);
			$user_id = $this->session->userdata('user_id');

			$this->view_data['goods_array'] = $this->config->item('goods_array');

			if(isset($goods_id))
			{
				// 상품관리자 추가(2020.06.06)
				if($this->view_data['auth_code'] == 12)
				{
					$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
				}

				//echo $_SERVER["REMOTE_ADDR"];
				// 등록 상품 정보 확인
				$sql = "SELECT
							GS.*,
							GSD.*,
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14, GSI.img15, GSI.img16, GSI.img17, GSI.img18, GSI.img19, GSI.img_etc0, GSI.img_etc1, GSI.img_etc2, GSI.img_etc3, GSI.img_etc4, GSI.img_etc5, GSI.img_etc6, GSI.img_etc7, GSI.img_etc8, GSI.img_etc9
							FROM
								goods AS		GS LEFT OUTER JOIN
								goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
								goods_image		As GSI ON GS.id = GSI.goods_id
							WHERE GS.id='{$goods_id}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();
				//debug($row);

				// 회원 상품이 맞는지 확인
				if($row->user_id == $user_id)
				{
					$this->view_data['Goods'] = $row;
					// debug($this->view_data['Goods']);
					$this->view_data['MarketName'] = $this->config_vars['open_market2'][$row->market];

					//debug($row->Category1);
					$this->view_data['cate1'] = $this->get_category('', false); // json_decode()
					// debug($this->view_data['cate1']);

					//debug($row->Category2);
					if($row->Category2)
					{
						$this->view_data['cate2'] = json_decode($this->get_category($row->Category2, false)); // json_decode()
						// debug($this->view_data['cate2']);

						//debug($row->Category3);
						$Category2_arr = explode('|', $row->Category2);
						$this->view_data['cate3'] = json_decode($this->get_category($Category2_arr[0].'|2', false)); // json_decode()
						// debug($this->view_data['cate3']);
					}
					else
						$this->view_data['cate2'] = json_decode($this->get_category($row->Category1, false)); // json_decode()
						//debug($this->view_data['cate2']);

					$this->view_data['Goods']->GoodsEtcUnserializes = [];
					if($this->view_data['Goods']->GoodsEtcSerializes) {
						$this->view_data['Goods']->OptionSizeArr = explode(',', $this->view_data['Goods']->OptionSize);
						$this->view_data['Goods']->GoodsEtcUnserializes = unserialize($this->view_data['Goods']->GoodsEtcSerializes);
						// debug($this->view_data['Goods']->OptionSizeArr);
						// debug($this->view_data['Goods']->GoodsEtcUnserializes);
					}
				}
				else
					alert('회원님의 상품이 아닙니다!', '/products/index');
			}

			$this->load->helper('html');
			$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
			$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
			$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

			//debug($this->config_vars);
			// debug($this->view_data);

			$this->load->view('top_w', $this->view_data);
            if($_SERVER["REMOTE_ADDR"] == "218.157.131.10")
                $this->load->view('products/edit_form_test', $this->view_data);
            else
                $this->load->view('products/edit_form_test', $this->view_data);
			$this->load->view('bottom');
		}
		else
			alert('정상적인 접근이 아닙니다!', '/products/index');
	}

	// 리얼상품수정폼
	function real_editing()
	{
		if(@$this->uri->segment(3))
		{
			$goods_id = $this->uri->segment(3);
			$user_id = $this->session->userdata('user_id');

			if(isset($goods_id))
			{
				// 상품관리자 추가(2020.06.06)
				if($this->view_data['auth_code'] == 12)
				{
					$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
				}

				// 등록 상품 정보 확인
				$sql = "SELECT
							GS.*,
							GSD.GoodsOptVal, GSD.Description, GSD.NoticeItemCodes,
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
							FROM
								goods_cron			AS GS LEFT OUTER JOIN
								goods_detail_cron	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
								goods_image_cron	As GSI ON GS.id = GSI.goods_id
							WHERE GS.id='{$goods_id}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();

				// 회원 상품이 맞는지 확인
				if($row->user_id == $user_id)
				{
					$this->view_data['Goods'] = $row;
					$this->view_data['MarketName'] = $this->config_vars['open_market2'][$row->market];
				}
				else
					alert('회원님의 상품이 아닙니다!', '/products/index');
			}

			$this->load->helper('html');
			$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
			$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
			$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

			//debug($this->config_vars);
			//debug($this->view_data);

			$this->load->view('top', $this->view_data);
			$this->load->view('products/edit_form.php', $this->view_data);
			$this->load->view('bottom');
		}
		else
			alert('정상적인 접근이 아닙니다!', '/products/index');
	}

	// 상품 복사폼
	function copying()
	{
		if(@$this->uri->segment(3))
		{
			$goods_id = $this->uri->segment(3);
			$user_id = $this->session->userdata('user_id');

			if(isset($goods_id))
			{
				// 상품관리자 추가(2020.06.06)
				if($this->view_data['auth_code'] == 12)
				{
					$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
				}

				// 등록 상품 정보 확인
				$sql = "SELECT
							GS.*,
							GSD.GoodsOptVal, GSD.Description, GSD.NoticeItemCodes,
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
							FROM
								goods AS		GS LEFT OUTER JOIN
								goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
								goods_image		As GSI ON GS.id = GSI.goods_id
							WHERE GS.id='{$goods_id}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();

				// 회원 상품이 맞는지 확인
				if($row->user_id == $user_id)
				{
					$this->view_data['Goods'] = $row;
					$this->view_data['MarketName'] = $this->config_vars['open_market2'][$row->market];
				}
				else
					alert('회원님의 상품이 아닙니다!', '/products/index');
			}

			$this->load->helper('html');
			$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
			$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');

			//debug($this->config_vars);
			//debug($this->view_data);

			$this->load->view('top', $this->view_data);
			$this->load->view('products/copy_form.php', $this->view_data);
			$this->load->view('bottom');
		}
		else
			alert('정상적인 접근이 아닙니다!', '/products/index');
	}

	// 상품수정 이미지 처리
	function image_process()
	{
		$output = array();
		if(@$this->uri->segment(3))
		{
			$gb = $this->uri->segment(3);
			$user_id = $this->session->userdata('user_id');

			// 상품관리자 추가(2020.06.06)
			if($this->view_data['auth_code'] == 12)
			{
				$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
			}

			// 관리자 상품등록 시 적용(2017.03.04) - 선택된 등록회원별 이미지 등록 경로를 위해
			if($this->input->post('UserID'))
			{
				$user_id = $this->input->post('UserID');
				$this->tmp_upload_url = $this->config->item('user_temp_image_dir').$this->input->post('UserID');
			}

			$goods_id = $this->input->post('id'); // 상품 일련번호
			$key = $this->input->post('key'); // 삭제할 이미지 필드명
			//$del_img_name = $this->input->post('name'); // 삭제할 이미지명

			// 상품이미지 삭제
			if($gb == 'd')
			{
				// 등록 상품 정보 확인
				$sql = "SELECT
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14, GSI.img15, GSI.img16, GSI.img17, GSI.img18, GSI.img19
							FROM goods_image As GSI
							WHERE GSI.goods_id='{$goods_id}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();
				//debug($row);

				// 삭제할 필드의 이미지명이 같은지 확인
				if($row->{$del_field} == $del_img_name)
				{
					$del_img_name_arr = explode('.', $del_img_name);
					$del_img_thumb_name = $del_img_name_arr[0].'_thumb.'.$del_img_name_arr[1];

					$del_files_url1 = $this->tmp_upload_url.'/'.$del_img_name;
					$del_files_url2 = $this->tmp_upload_url.'/'.$del_img_thumb_name;
					//debug($del_files_url1);
					//debug($del_files_url2);

					//delete_files($del_files_url1);
					if(file_exists($del_files_url1))
						$output['uploaded'] = 'OK';
					else
						$output['uploaded'] = 'ERROR';
				}
				else
					$output['uploaded'] = 'ERROR';

				/*
				$img_path = '/data/files/goods/'.$user_id.'/';
				$img_cnt = 0;
				$initial_preview = '';
				$initial_preview_config = '';
				for($i=0; $i<15; $i++)
				{
					$img_name = $row->{'img'.$i};

					if($key == $i)
					{
						$initial_preview .= '"<img style=\'height:160px\' src=\''.$img_name.'\'>",';
						$initial_preview_config .= '{"caption": "메인이미지", "width": "160px", "url": "/products/image_process/d", "key": "'.$i.'"},';
					}
					else
					{
						if($img_name)
						{
							//echo $img_name;
							$initial_preview .= '"<img style=\'height:160px\' src=\''.$img_name.'\'>",';
							$initial_preview_config .= '{"caption": "'.$img_name.'", "width": "160px", "url": "/products/image_process/d", "key": "'.$i.'", "extra": {"id":'.$goods_id.',"name":"'.$img_name.'"}},';
						}
						else
						{
							$initial_preview .= '"<img style=\'height:160px\' src=\'\'>",';
							$initial_preview_config .= '{"caption": "", "width": "160px", "url": "/products/image_process/d", "key": "'.$i.'"},';
						}
					}
				}
				//echo $initial_preview.'<br>';
				//echo $initial_preview_config;
				//$initial['append'] = false;
				//$initial_preview = stripslashes(json_encode($initial['initial_preview']));
				//$initial_preview_config = stripslashes(json_encode($initial['initial_preview_config']));
				//substr_replace( $initial_preview, "", -1 );

				echo '{"initialPreview": ['.substr_replace( $initial_preview, "", -1 ).'], "initialPreviewConfig": ['.substr_replace( $initial_preview_config, "", -1 ).'],"append":false}';
				//echo stripslashes(json_encode($initial));

				//$test = json_encode(['initialPreview' => [$initial_preview],'initialPreviewConfig' => [$initial_preview_config],'append' => false]);
				*/
				echo json_encode($output);
			}
			// 상품이미지 수정 임시저장
			if($gb == 'm')
			{
				//debug($_FILES);exit;
				//debug($this->tmp_upload_url);

				$config['upload_path'] = $this->tmp_upload_url;
				$config['allowed_types'] = 'gif|jpg|jpeg|png';
				$config['overwrite']	= TRUE;
				//$this->load->library('upload', $config);

				if(empty($_FILES['GoodsImage']))
				{
					//echo json_encode(['error'=>'No files found for upload.']);
					// or you can throw an exception
					return; // terminate
				}

				$images = $_FILES['GoodsImage'];
				//debug($images);exit;
				$success = null;
				$paths = array();
				$filenames = $images['name'];

				// loop and process files
				$img_path = '/data/files/goods/'.$user_id.'/';
				for($i=0; $i < count($filenames); $i++)
				{
					// 파일명 변환
					$name_arr = explode('.', $filenames[$i]);
					$rand_name = md5(uniqid(mt_rand(), true)).'.'.$name_arr[1];
					$new_filename = $rand_name;
					$target = $this->tmp_upload_url."/".$new_filename;
					// if(move_uploaded_file($images['tmp_name'][$i], $target))
					if($this->compressImage($images['tmp_name'][$i], $target, 70))
					{
						$success = true;
						$paths[$i]['path'] = $img_path.$new_filename;
						$paths[$i]['name'] = $new_filename;
					}
					else
					{
						$success = false;
						break;
					}
				}

				if ($success === true)
				{
					$output['uploaded'] = $paths;
				}
				elseif ($success === false)
				{
					$output['error'] = '이미지를 업로드하는 중 오류가 발생했습니다.';
					// delete any uploaded files
					foreach ($paths as $file) {
						unlink($file);
					}
				}
				else {
					$output['error'] = '처리된 파일이 없습니다.';
				}

				echo json_encode($output);
			}

			// 상품이미지 등록/복사 임시저장
			if($gb == 'u')
			{
				// debug($_FILES);
				//debug($this->tmp_upload_url);

				// $name_arr = explode('.', $_FILES['GoodsImage']['name']);
				// $rand_name = md5(uniqid(mt_rand(), true)).'.'.$name_arr[1];
				// $_FILES['GoodsImage']['name'] = $rand_name;
				// debug($_FILES);

				// $config['encrypt_name'] = TRUE;
				// $config['upload_path'] = $this->tmp_upload_url;
				// $config['allowed_types'] = 'gif|jpg|jpeg|png';
				// $config['overwrite']	= TRUE;
				// // $config['file_name'] = $rand_name;
				// $this->load->library('upload', $config);

				// $field_name = "GoodsImage";
				// if(!$this->upload->do_upload($field_name))
				// {
				// 	$error = array('error' => $this->upload->display_errors());
				// 	//debug($error);

				// 	$output['uploaded'] = 'ERROR';
				// }
				// else
				// {
				// 	$data = array('upload_data' => $this->upload->data());
				// 	// debug($data);

				// 	// $output['orname'] = $_FILES['GoodsImage']['name'];
				// 	// $output['rename'] = $data['upload_data']['file_name'];
				// 	$output['uploaded'] = 'OK';
				// }

				// if($_SERVER["REMOTE_ADDR"] == "218.157.131.10")
					$output = $this->image_rename_test($_FILES, 'GoodsImage');
				// else
					// $output = $this->image_rename($_FILES, 'GoodsImage');

				echo json_encode($output);
			}
		}
	}

	// Compress image
	function compressImage($source, $destination, $quality)
	{
		$rtn = false;
		$info = @getimagesize($source);
		if ($info === false) {
			return false;
		}
		// debug($info);

		if ($info['mime'] == 'image/jpeg') {
			$image = @imagecreatefromjpeg($source);
			if (!$image) {
				return $rtn;
			}
			imageinterlace($image, 1);
			$rtn = imagejpeg($image, $destination, $quality);
		}

		if ($info['mime'] == 'image/gif') {
			$image = @imagecreatefromgif($source);
			if (!$image) {
				return $rtn;
			}
			$rtn = imagegif($image, $destination, $quality);
		}

		if ($info['mime'] == 'image/png') {
			$image = @imagecreatefrompng($source);
			if (!$image) {
				return $rtn;
			}
			$rtn = imagepng($image, $destination, $quality);
		}

		// debug($test);exit;

		return $rtn;
	}

    // 상품코드 관리 이미지 압축(2021.02.07) 추가
    function goods_code_image_compress()
    {
        $userpath = '/home/danharoo/www';

		// products lib 생성
		$this->load->library('products_handler');
        $this->load->model('admin_m');

		$file = $this->input->post('file');

		// 다중 처리
		if(is_array($file))
		{
			// debug($file);exit;
			$file_cnt = count($file);
            $loop_cnt = 0;
			$success_cnt = 0;
			foreach($file AS $k => $v)
			{
				// debug($v);
				$logs = [];
				$rtn = 0;
                $file = $v['url'];
                if(!strpos($file, $userpath)) $file = $userpath.$v['url'];
    	        $rtn = $this->products_handler->goods_image_compress($file);
				// debug($rtn);
				if($rtn === 1) {
					$stack1 = explode('/', $file);
					$logs['file'] = $file;
					$logs['filename'] = array_pop($stack1);
					$logs['goodscode'] = array_pop($stack1);
					$logs['dirname'] = implode('/', $stack1);
        			$logs['ofileinfo'] = $this->products_handler->goods_image_info_return($file);
					// debug($logs);
    				$this->admin_m->goods_code_image_compress_history_log($logs);

					$success_cnt++;
				}
                $loop_cnt++;
			}

			if($file_cnt == $loop_cnt)
			{
				echo '{"info":{"success":true,"text":"선택이미지 ['.$file_cnt.' 개] 중 ['.$success_cnt.' 개] 이미지가 처리되었습니다."}}';
				exit;
			}
		}
		// 단일 처리
		else
		{
			$logs = [];
            $rtn = 0;
            if(!strpos($file, $userpath)) $file = $userpath.$file;
            // debug($file);exit;
	        $rtn = $this->products_handler->goods_image_compress($file);
			if($rtn === 1) {
    			$stack1 = explode('/', $file);
    			$logs['file'] = $file;
    			$logs['filename'] = array_pop($stack1);
                $logs['goodscode'] = array_pop($stack1);
    			$logs['dirname'] = implode('/', $stack1);
    			$logs['ofileinfo'] = $this->products_handler->goods_image_info_return($file);
				// debug($logs);exit;
				// $logs['cfileinfo'] = getimagesize($file);
				$this->admin_m->goods_code_image_compress_history_log($logs);
			}

			echo $this->products_handler->goods_image_compress_return_txt($rtn);
		}
    }

	// 이미지 네임 변경(2021.01.27)
	function image_rename_test($_file, $gb)
	{
		// debug($_file);exit;
		$images = $_file[$gb];

		if(empty($images)) return;

		//debug($images);exit;
		$success = null;
		$output = [];
		$filenames = $images['name']; // 원본 파일명

		// 파일명 변환
		$name_arr = explode('.', $filenames);
		$rand_name = md5(uniqid(mt_rand(), true)).'.'.$name_arr[1];
		$new_filename = $rand_name;

		// if($rtnname) return $output;

		$target = $this->tmp_upload_url."/".$new_filename;
		// $test = $this->compressImage($images['tmp_name'], $target, 60);
		// debug($test);exit;
		if($this->compressImage($images['tmp_name'], $target, 70))
		{
			$output['orname'] = $filenames;
			$output['rename'] = $new_filename;
			$output['uploaded'] = 'OK';
		}
		else
		{
			$output['uploaded'] = 'ERROR';
		}

		return $output;
	}

	// 이미지 네임 변경(2021.01.27)
	function image_rename($_file, $gb)
	{
		// debug($_file);exit;
		$images = $_file[$gb];

		if(empty($images)) return;

		//debug($images);exit;
		$success = null;
		$output = [];
		$filenames = $images['name']; // 원본 파일명

		// 파일명 변환
		$name_arr = explode('.', $filenames);
		$rand_name = md5(uniqid(mt_rand(), true)).'.'.$name_arr[1];
		$new_filename = $rand_name;

		// if($rtnname) return $output;

		$target = $this->tmp_upload_url."/".$new_filename;
		if(move_uploaded_file($images['tmp_name'], $target))
		{
			$output['orname'] = $filenames;
			$output['rename'] = $new_filename;
			$output['uploaded'] = 'OK';
		}
		else
		{
			$output['uploaded'] = 'ERROR';
		}

		return $output;
	}

	// 상품수정 참고용이미지 처리(2020.12.13)
	function image_etc_process()
	{
		$output = array();
		if(@$this->uri->segment(3))
		{
			$gb = $this->uri->segment(3);
			$user_id = $this->session->userdata('user_id');

			// 상품관리자 추가(2020.06.06)
			if($this->view_data['auth_code'] == 12)
			{
				$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
			}

			// 관리자 상품등록 시 적용(2017.03.04) - 선택된 등록회원별 이미지 등록 경로를 위해
			if($this->input->post('UserID'))
			{
				$user_id = $this->input->post('UserID');
				$this->tmp_upload_url = $this->config->item('user_temp_image_dir').$this->input->post('UserID');
			}

			$goods_id = $this->input->post('id'); // 상품 일련번호
			$key = $this->input->post('key'); // 삭제할 이미지 필드명
			//$del_img_name = $this->input->post('name'); // 삭제할 이미지명

			// 상품이미지 삭제
			if($gb == 'd')
			{
				// 등록 상품 정보 확인
				$sql = "SELECT
							GSI.img_etc0, GSI.img_etc1, GSI.img_etc2, GSI.img_etc3, GSI.img_etc4, GSI.img_etc5, GSI.img_etc6, GSI.img_etc7, GSI.img_etc8, GSI.img_etc9
							FROM goods_image As GSI
							WHERE GSI.goods_id='{$goods_id}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();
				//debug($row);

				// 삭제할 필드의 이미지명이 같은지 확인
				if($row->{$del_field} == $del_img_name)
				{
					$del_img_name_arr = explode('.', $del_img_name);
					$del_img_thumb_name = $del_img_name_arr[0].'_thumb.'.$del_img_name_arr[1];

					$del_files_url1 = $this->tmp_upload_url.'/'.$del_img_name;
					$del_files_url2 = $this->tmp_upload_url.'/'.$del_img_thumb_name;
					//debug($del_files_url1);
					//debug($del_files_url2);

					//delete_files($del_files_url1);
					if(file_exists($del_files_url1))
						$output['uploaded'] = 'OK';
					else
						$output['uploaded'] = 'ERROR';
				}
				else
					$output['uploaded'] = 'ERROR';

				echo json_encode($output);
			}
			// 상품이미지 수정 임시저장
			if($gb == 'm')
			{
				//debug($_FILES);exit;
				//debug($this->tmp_upload_url);

				$config['upload_path'] = $this->tmp_upload_url;
				$config['allowed_types'] = 'gif|jpg|jpeg|png';
				$config['overwrite']	= TRUE;
				//$this->load->library('upload', $config);

				if(empty($_FILES['GoodsImageEtc']))
				{
					//echo json_encode(['error'=>'No files found for upload.']);
					// or you can throw an exception
					return; // terminate
				}

				$images = $_FILES['GoodsImageEtc'];
				//debug($images);exit;
				$success = null;
				$paths = array();
				$filenames = $images['name'];

				// loop and process files
				$img_path = '/data/files/goods/'.$user_id.'/';
				for($i=0; $i < count($filenames); $i++)
				{
					//$ext = explode('.', basename($filenames[$i]));
					// 파일명 변환
					$name_arr = explode('.', $filenames[$i]);
					$rand_name = md5(uniqid(mt_rand(), true)).'.'.$name_arr[1];
					$new_filename = $rand_name;
					$target = $this->tmp_upload_url."/".$new_filename;
					// if(move_uploaded_file($images['tmp_name'][$i], $target))
					if($this->compressImage($images['tmp_name'][$i], $target, 70))
					{
						$success = true;
						$paths['path'] = $img_path.$new_filename;
						$paths['name'] = $new_filename;
					}
					else
					{
						$success = false;
						break;
					}
				}

				if ($success === true)
				{
					$output['uploaded'] = $paths;
				}
				elseif ($success === false)
				{
					$output['error'] = '이미지를 업로드하는 중 오류가 발생했습니다.';
					// delete any uploaded files
					foreach ($paths as $file) {
						unlink($file);
					}
				}
				else {
					$output['error'] = '처리된 파일이 없습니다.';
				}

				echo json_encode($output);
			}

			// 상품이미지 등록/복사 임시저장
			if($gb == 'u')
			{
				// debug($_FILES);
				//debug($this->tmp_upload_url);

				// $name_arr = explode('.', $_FILES['GoodsImage']['name']);
				// $rand_name = md5(uniqid(mt_rand(), true)).'.'.$name_arr[1];
				// $_FILES['GoodsImage']['name'] = $rand_name;
				// debug($_FILES);

				// $config['encrypt_name'] = TRUE;
				// $config['upload_path'] = $this->tmp_upload_url;
				// $config['allowed_types'] = 'gif|jpg|jpeg|png';
				// $config['overwrite']	= TRUE;
				// // $config['file_name'] = $rand_name;
				// $this->load->library('upload', $config);

				// $field_name = "GoodsImageEtc";
				// if(!$this->upload->do_upload($field_name))
				// {
				// 	$error = array('error' => $this->upload->display_errors());
				// 	//debug($error);

				// 	$output['uploaded'] = 'ERROR';
				// }
				// else
				// {
				// 	$data = array('upload_data' => $this->upload->data());

				// 	$output['uploaded'] = 'OK';
				// }

				$output = $this->image_rename_test($_FILES, 'GoodsImageEtc');

				echo json_encode($output);
			}
		}
	}

	// 주문옵션 관리
	function order_option()
	{
		$user_id = $this->session->userdata('user_id');

		$this->load->view('products/order_option.php', $this->view_data);
	}

	// 상품등록 폼
	function create()
	{
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
		$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$user_id = $this->session->userdata('user_id');

        $this->view_data['goods_array'] = $this->config->item('goods_array');
		// 상품 사이즈 목록, 2022.08.31
		$this->load->model('goods_m');
		$this->view_data['goods_size_cate'] = $this->goods_m->get_goods_size_value();
		$this->view_data['goods_model_list'] = $this->goods_m->get_goods_model_info();
		$this->view_data['goods_color_list'] = $this->goods_m->get_goods_color_list();
		$this->view_data['goods_fit_list'] = $this->goods_m->get_goods_fit_list();
		$this->view_data['etc'] = $this->goods_m->get_goods_option_etc();

		// 카테고리 목록
		$this->view_data['cate1'] = $this->get_category();
		//debug($this->config_vars);
		//debug($this->view_data);

		// 상품코드 자동생성 연도 월
		$this->view_data['GoodsCode_4'] = substr(date('Y', time()), -1); // 연도 끝자리
		$this->view_data['GoodsCode_5'] = date('n', time()); // 월
		if($this->view_data['GoodsCode_5'] == 10) $this->view_data['GoodsCode_5'] = 'a';
		if($this->view_data['GoodsCode_5'] == 11) $this->view_data['GoodsCode_5'] = 'b';
		if($this->view_data['GoodsCode_5'] == 12) $this->view_data['GoodsCode_5'] = 'c';

        // 제조일 = 해당 등록일자 자동입력(2021.10.15)
		$this->view_data['GoodsEtc20'] = date('Ymd', time()); // 년월일

		$this->load->view('top', $this->view_data);
		$this->load->view('products/in_form.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품 사이즈표 정보, 2022.08.31
	function get_size_graph() {
		$gsi_no = isset($_REQUEST['no']) ? $_REQUEST['no']:'';

		if($gsi_no == '') {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$this->load->model('goods_m');
		$result = $this->goods_m->get_goods_size_graph_value($gsi_no);
		
		$json_result = json_encode($result, JSON_UNESCAPED_UNICODE);

		if($result) {
			echo '{"info":{"success":true,"text":"성공","data":'.$json_result.'}}';
		}else {
			echo '{"info":{"success":false,"text":"실패, 다시 한 번 시도해주세요"}}';
		}
	}

	// 상품등록 폼
	function create_test()
	{
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
		$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$user_id = $this->session->userdata('user_id');

        $this->view_data['goods_array'] = $this->config->item('goods_array');

		// 카테고리 목록
		$this->view_data['cate1'] = $this->get_category();
		//debug($this->config_vars);
		//debug($this->view_data);

		// 상품코드 자동생성 연도 월
		$this->view_data['GoodsCode_4'] = substr(date('Y', time()), -1); // 연도 끝자리
		$this->view_data['GoodsCode_5'] = date('n', time()); // 월
		if($this->view_data['GoodsCode_5'] == 10) $this->view_data['GoodsCode_5'] = 'a';
		if($this->view_data['GoodsCode_5'] == 11) $this->view_data['GoodsCode_5'] = 'b';
		if($this->view_data['GoodsCode_5'] == 12) $this->view_data['GoodsCode_5'] = 'c';

		$this->load->view('top', $this->view_data);
		if($_SERVER["REMOTE_ADDR"] == "218.157.131.10")
			$this->load->view('products/in_form.php', $this->view_data);
		else
			$this->load->view('products/in_form_test.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 단하루 카테고리 정보
	function get_category($code='', $url=true)
	{
		$rtn = true;

		if($url && @$this->uri->segment(3))
		{
			$rtn = false;
			$code = $this->uri->segment(3);
			if(@$this->uri->segment(4)) $code .= '|'.$this->uri->segment(4);
		}
		//debug('code : '.$code);

        // 카테고리 활성화(activated='Y:사용, N:미사용') 검색 필드 추가 (2017.08.03)
		if($code)
		{
			$code_arr = explode('|', $code);
			//debug($code_arr);

			if(count($code_arr) == 1)
			{
				//debug($code);
				$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code}");
				//echo "SELECT * FROM goods_cate WHERE market='H' and id={$code}";
				if($query->num_rows() > 0)
				{
					$row = $query->row();
					//debug($row);

					$query = $this->db->query("SELECT id, MiddleCategory, hashtag FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' and activated='Y' group by MiddleCategory order by MiddleCode asc");
					$i = 0;
					foreach ($query->result() as $row)
					{
						if($row->MiddleCategory)
						{
							$data[$i]['Code'] = $row->id;
							$data[$i]['Name'] = $row->MiddleCategory;
							$data[$i]['Tag'] = $row->hashtag;
							$i++;
						}
					}

					if(count($data) > 0)
						$data = json_encode($data);
					else
						$data = '[]';
				}
			}
			else
			{
				if($code_arr[1] == '1')
				{
					//debug($code);
					$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code_arr[0]}");
					//echo "SELECT * FROM goods_cate WHERE market='H' and id={$code_arr[0]}";
					if($query->num_rows() > 0)
					{
						$row = $query->row();
						//debug($row);

						$query = $this->db->query("SELECT id, MiddleCategory, hashtag FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' and activated='Y' group by MiddleCategory order by MiddleCode asc");
						$i = 0;
						foreach ($query->result() as $row)
						{
							if($row->MiddleCategory)
							{
								$data[$i]['Code'] = $row->id;
								$data[$i]['Name'] = $row->MiddleCategory;
								$data[$i]['Tag'] = $row->hashtag;
								$i++;
							}
						}

						if(count($data) > 0)
							$data = json_encode($data);
						else
							$data = '[]';
					}
				}
				elseif($code_arr[1] == '2')
				{
					$code = $code_arr[0];
					$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code}");
					if($query->num_rows() > 0)
					{
						$row = $query->row();

						$query = $this->db->query("SELECT id, SmallCategory, CategoryCode, hashtag FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' and MiddleCategory='{$row->MiddleCategory}' and activated='Y' group by SmallCategory order by SmallCode asc");
						$i = 0;
						foreach ($query->result() as $row)
						{
							if($row->SmallCategory)
							{
								$data[$i]['Code'] = $row->id;
								$data[$i]['Name'] = $row->SmallCategory;
								$data[$i]['Tag'] = $row->hashtag;
								$i++;
							}
						}

						if(count($data) > 0)
							$data = json_encode($data);
						else
							$data = '[]';
					}
				}
				else $data = '[]';
			}

		}
		else
		{
			$query = $this->db->query("SELECT id, LargeCategory FROM goods_cate WHERE market='H' and activated='Y' group by LargeCategory order by LargeCode asc");
			if($query->num_rows() > 0)
			{
				$i = 0;
				foreach ($query->result() as $row)
				{
					$data[$i]['Code'] = $row->id;
					$data[$i]['Name'] = $row->LargeCategory;
					$i++;
				}
			}
			//print_r($data);
		}

		//debug($data);

		if($rtn)
			return $data;
		else
			echo $data;
	}

	// 선택 중분류 시작연번 확인
	function get_category2()
	{
		$rtn_num = 0;

		$code = $this->uri->segment(3);

		if(!$code)
		{
			echo 'error';
			exit;
		}

		$query = $this->db->query("SELECT MAX(GoodsCode_2) as num FROM goods WHERE GoodsCode_1='{$code}'");
		$row = $query->row();

		$rtn_num = $row->num;
		// debug($rtn_num);

		if($rtn_num > 0) echo (int)$rtn_num + 1;
		else echo '';
	}

	// 상품등록
	function update()
	{
		$FalseCnt = 0; // 오류 카운트
		//$this->load->library('image_lib');

		//$config['image_library'] = 'gd2';
		//$config['maintain_ratio'] = TRUE;

		$user_id = $this->session->userdata('user_id');

		// 폼 데이타 변수 담기
		$data = $this->input->post();

		$data_gb_txt = '';

		// STEP2-v1.3: 저장 시점에 사전 INSERT + GoodsName/GoodsEtc38 반영 (신규·수정 모드 공통 — DanharooGoodsName 입력 시)
		$dan = isset($data['DanharooGoodsName']) ? trim($data['DanharooGoodsName']) : '';
		$code = isset($data['GoodsCode']) ? trim($data['GoodsCode']) : '';
		if ($dan !== '' && $code !== '') {
			$this->load->helper('hangul_shift');
			$code6 = isset($data['GoodsCode_6']) ? trim($data['GoodsCode_6']) : null;
			$out = get_newtalk_auto_names($dan, $code, $this->db, $code6, true);
			$data['GoodsName'] = $out['goodsName'];
			$data['GoodsEtc38'] = $out['coupangName'];
		}

		// 신규등록
		if($data['GoodsCmd'] == '1')
		{
			// 마스터 상품 등록 처리
			$data['DataRtn'] = '1'; // 마스터키 리턴 체크
			$data['GdsMstId'] = $this->update_master($data);
		}

		// 복사
		if($data['GoodsCmd'] == '3')
		{
			$data['DataRtn'] = '1';
		}

		// 상품 등록/수정/복사 처리 함수
		$this->update_process($data);

	}

	// 상품등록 테스트
	function update_test()
	{
//				 error_reporting( E_ALL );
//		  ini_set( "display_errors", 1 );
		$FalseCnt = 0; // 오류 카운트
		//$this->load->library('image_lib');

		//$config['image_library'] = 'gd2';
		//$config['maintain_ratio'] = TRUE;

		$user_id = $this->session->userdata('user_id');

		// 폼 데이타 변수 담기
		$data = $this->input->post();
		// if($_SERVER["REMOTE_ADDR"] == "218.157.131.10") {debug($data);exit;}

		$data_gb_txt = '';

		// 신규상품별 직렬데이타 처리(2020.11.12)
		$GoodsEtcSerializes = [];

		if(isset($data['OptionSizeKey'])) $GoodsEtcSerializes['OptionSizeKey'] = $data['OptionSizeKey'];

		$OptionSizeValueArr = [];	// OptionSizeValue Data 가공된 변수

		$this->load->model('goods_m');
		$etc = $this->goods_m->get_goods_option_etc();

        // 사이즈표 사이즈 타이틀 추가에 따른 데이타 반영(2021.10.03)
		// $sz_value_arr = $goods_array['sz_value'][$data['OptionSizeKey']];
		$sz_value_arr = $data['OptionSizeTitle'];
		$sz_en_value_arr = $data['OptionSizeEnTitle']; // 2021.10.04
		// if($_SERVER["REMOTE_ADDR"] == "218.157.131.10") {debug($sz_value_arr);exit;}

		// 수정된 사이즈표 사이즈값으로 다시 가공
		$GoodsEtc14Txt = '';
		$OptionSizeValueCount = 0;
		if(is_array($sz_value_arr[0]))
		{
			// 세트 사이즈일 때
			// foreach( $goods_array['sz_value'][$data['OptionSizeKey']] as $k1 => $v1 )
			foreach( $sz_value_arr as $k1 => $v1 )
			{
				foreach( array_unique($data['OptionSizeArr']) as $k2 => $v2 )
				{
					foreach( $sz_value_arr[$k1] as $k3 => $v3 )
					{
						$OptionSizeValueArr[$k2][$k1][] = $data['OptionSizeValue'][$OptionSizeValueCount];
						$OptionSizeValueCount++;
					}
				}
			}

			// foreach( $goods_array['sz_value'][$data['OptionSizeKey']] as $k1 => $v1 )
			foreach( $sz_value_arr as $k1 => $v1 )
			{
				if($k1 == 0) {
					if($data['OptionSizeKey'] == '16')
						$GoodsEtc14Txt .= '[ 나그랑/가오리 ]'.PHP_EOL;
					else
						$GoodsEtc14Txt .= '[ 기본상의 ]'.PHP_EOL;
					$GoodsEtc14Txt .= implode(' / ', $sz_value_arr[0]).PHP_EOL;
				}
				if($k1 == 1) {
					if($data['OptionSizeKey'] == '6' || $data['OptionSizeKey'] == '18') $GoodsEtc14Txt .= '[ 바지 ]'.PHP_EOL;
					if($data['OptionSizeKey'] == '7' || $data['OptionSizeKey'] == '19') $GoodsEtc14Txt .= '[ 스커트 ]'.PHP_EOL;
					if($data['OptionSizeKey'] == '8' || $data['OptionSizeKey'] == '26') $GoodsEtc14Txt .= '[ 기본상의 ]'.PHP_EOL;
					if($data['OptionSizeKey'] == '15' || $data['OptionSizeKey'] == '16') $GoodsEtc14Txt .= '[ 원피스 ]'.PHP_EOL;
					$GoodsEtc14Txt .= implode(' / ', $sz_value_arr[1]).PHP_EOL;
				}
				if($k1 == 2) {
					if($data['OptionSizeKey'] == '26') $GoodsEtc14Txt .= '[ 바지 ]'.PHP_EOL;
					$GoodsEtc14Txt .= implode(' / ', $sz_value_arr[2]).PHP_EOL;
				}

				foreach( array_unique($data['OptionSizeArr']) as $k2 => $v2 )
				{
					$GoodsEtc14Txt .= '('.$v2.')' . implode(' / ', $OptionSizeValueArr[$k2][$k1]).PHP_EOL;
				}
			}
		}
		else
		{
			$GoodsEtc14Txt .= implode(' / ', $sz_value_arr).PHP_EOL;

			foreach( $data['OptionSizeArr'] as $k1 => $v1 )
			{
				foreach( $sz_value_arr as $k2 => $v2 )
				{
					$OptionSizeValueArr[$k1][] = $data['OptionSizeValue'][$OptionSizeValueCount];
					$OptionSizeValueCount++;
				}

				$GoodsEtc14Txt .= '('.$v1.')' . implode(' / ', $OptionSizeValueArr[$k1]).PHP_EOL;
			}
		}

		$data['GoodsEtc14'] = $GoodsEtc14Txt;
		// debug($GoodsEtc14Txt);exit;

		// debug($GoodsEtc14Txt);debug($OptionSizeValueArr);exit;

        // 사이즈표 타이틀 추가(2021.09.30)
		if(isset($data['OptionSizeTitle'])) $GoodsEtcSerializes['OptionSizeTitle'] = $data['OptionSizeTitle'];
        // 사이즈표 영문 타이틀 추가(2021.10.04)
		if(isset($data['OptionSizeEnTitle'])) $GoodsEtcSerializes['OptionSizeEnTitle'] = $data['OptionSizeEnTitle'];

		$GoodsEtcSerializes['OptionSizeValue'] = $OptionSizeValueArr;
		// debug($GoodsEtcSerializes);exit;

		// 사이즈값도 가공된 사이즈표 사이즈로 다시 가공
		if(count($data['OptionSizeArr']) > 0)
			$data['OptionSize'] = implode(',', array_unique($data['OptionSizeArr']));
		// debug($GoodsEtcSerializes);debug($data);exit;

		if(isset($data['CheckPoint1'])) $GoodsEtcSerializes['CheckPoint1'] = $data['CheckPoint1'];
		if(isset($data['CheckPoint2'])) $GoodsEtcSerializes['CheckPoint2'] = $data['CheckPoint2'];
		if(isset($data['CheckPoint3'])) $GoodsEtcSerializes['CheckPoint3'] = $data['CheckPoint3'];
		if(isset($data['CheckPoint4'])) $GoodsEtcSerializes['CheckPoint4'] = $data['CheckPoint4'];
		if(isset($data['CheckPoint5'])) $GoodsEtcSerializes['CheckPoint5'] = $data['CheckPoint5'];
		if(isset($data['CheckPoint6'])) $GoodsEtcSerializes['CheckPoint6'] = $data['CheckPoint6'];

		$data['GoodsEtcSerializes'] = '';
		if(count($GoodsEtcSerializes) > 0) $data['GoodsEtcSerializes'] = serialize($GoodsEtcSerializes);
		// debug($data['GoodsEtcSerializes']);
		// $unserialize = unserialize($data['GoodsEtcSerializes']);
		// debug($unserialize);

		$GoodsEtc15Txt = '';
		if($data['CheckPoint1']) $GoodsEtc15Txt .= '안감(' . $etc['lining'][$data['CheckPoint1']] . '),';
		if($data['CheckPoint2']) $GoodsEtc15Txt .= '촉감(' . $etc['texture'][$data['CheckPoint2']] . '),';
		if($data['CheckPoint3']) $GoodsEtc15Txt .= '착용감(' . $etc['fit'][$data['CheckPoint3']] . '),';
		if($data['CheckPoint4']) $GoodsEtc15Txt .= '신축성(' . $etc['stretch'][$data['CheckPoint4']] . '),';
		if($data['CheckPoint5']) $GoodsEtc15Txt .= '두께감(' . $etc['thinkness'][$data['CheckPoint5']] . '),';
		if($data['CheckPoint6']) $GoodsEtc15Txt .= '비침(' . $etc['elasticity'][$data['CheckPoint6']] . ')';
		$data['GoodsEtc15'] = $GoodsEtc15Txt;
		
		//테이블 기본값 오류로 인한 전처리
		if (!isset($data['GoodsCount'])) $data['GoodsCount'] = 999;
		if ($data['GoodsCount'] == '') $data['GoodsCount'] = 999;
		if (!isset($data['model_id']) || $data['model_id'] === '') $data['model_id'] = 0;

		// 신규등록
		if($data['GoodsCmd'] == '1')
		{
			// 마스터 상품 등록 처리
			$data['DataRtn'] = '1'; // 마스터키 리턴 체크
			$data['GdsMstId'] = $this->update_master($data);
		}

		// 복사
		if($data['GoodsCmd'] == '3')
		{
			$data['DataRtn'] = '1';
		}
		
		/*var_dump($data);
		var_dump($etc);
		exit;*/

		// 상품 등록/수정/복사 처리 함수
		$this->update_process($data);
	}

	// 리얼 상품등록
	function real_update()
	{
		$FalseCnt = 0; // 오류 카운트
		//$this->load->library('image_lib');

		//$config['image_library'] = 'gd2';
		//$config['maintain_ratio'] = TRUE;

		$user_id = $this->session->userdata('user_id');

		// 폼 데이타 변수 담기
		$data = $this->input->post();
		//debug($data);

		$data_gb_txt = '';

		// 리얼 상품 수정 처리 함수
		$this->real_update_process($data);

	}

	// 상품 이미지 확인
	function goods_image_check($data)
	{
		if(isset($data['DataRtn'])) $DataRtn = true;
		else $DataRtn = false;

		// 상품 이미지 확인 및 등록
		if($data['GoodsImageList'])
		{
			$GoodsImageArr = explode('||', $data['GoodsImageList']);
			//debug($GoodsImageArr);
			$tmp_file_arr = get_filenames($this->tmp_upload_url); // 임시 이미지 배열
			//debug($tmp_file_arr);

			foreach($GoodsImageArr AS $k => $v)
			{
				if($v)
				{
					// 임시경로에 이미지 확인, 있으면 등록경로에 업로드
					if( in_array($v, $tmp_file_arr) )
					{
						// 메인이미지만 썸네일 저장
						//if($k == 0)
						//{
							$config['source_image']	= $this->tmp_upload_url.'/'.$v;
							$config['new_image']	= $this->tmp_upload_url.'/thumbnail/';
							if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

							if(!is_file($config['new_image'].$v))
							{
								// thumbnail 생성
								$this->load->library('image_lib');
								$config['image_library'] = 'gd2';
								$config['maintain_ratio'] = TRUE;
								$config['width']	= 100;
								$config['height']	= 100;
								//debug($config);
								$this->image_lib->initialize($config);
								$this->image_lib->resize();
							}
						//}
					}
					else
					{
						if($DataRtn) return '{"info":{"success":false,"error":{"kind":"img","key":"'.$k.'","msg":"임시이미지에 이미지가 없습니다."}}}';
						echo '{"info":{"success":false,"text":"이미지 등록 오류(2)입니다."}}';
						exit;
					}
				}
			}

			if($DataRtn) return $GoodsImageArr;
			echo '{"info":{"success":true}}';
		}
		else
		{
			if($DataRtn) return '{"info":{"success":false,"error":{"kind":"img","key":"A","msg":"이미지 데이타가 없습니다."}}}';
			echo '{"info":{"success":false,"text":"이미지 등록 오류(1)입니다."}}';
			exit;
		}

	}

	// 마스터 상품 등록 처리
	function update_master($data)
	{
		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(isset($data['DataRtn'])) $DataRtn = true;
		else $DataRtn = false;

		// 상품 이미지 확인
		$GoodsImageArr = $this->goods_image_check($data);
		// debug($GoodsImageArr);exit;

		//$SellingPeriodArr = explode("|", $data['SellingPeriod']);

		$insert_data = array(
					   'user_id'					=> $user_id,
					   'GoodsName'					=> $data['GoodsName'],
					   'GoodsCode'					=> $data['GoodsCode'],	// 상품코드 추가(2017.02.09)
					   'CatalogName'				=> '',
					   'BrandName'					=> '',
					   'MakerName'					=> $data['MakerName'],
					   'SellingPeriod'				=> '',
					   'SellingPeriodStart'			=> '',
					   'SellingPeriodEnd'			=> '',
					   'GoodsPrice'					=> $data['GoodsPrice'],
					   'GoodsCount'					=> $data['GoodsCount'],
					   'GoodsOptionsUseSetting'		=> 'N',
					   'GoodsImage'					=> $GoodsImageArr[0],
					   'CommonDeliveryWayOPTSEL'	=> '1',
					   'DeliveryCOMP'				=> '',
					   'ShipmentPlaceNo'			=> '',
					   'DeliveryFeeType'			=> '',
					   'NoticeItemGroupNo'			=> '',
					   'created'					=> $this->info['created']
					);
		$this->db->insert('goods_master', $insert_data);
		$GdsMstId = $this->db->insert_id();

		// 상품코드 등록 추가(2017.02.09)
		if($GdsMstId > 0 && $data['GoodsCode'] != '')
		{
			$goods_img_dir = $this->config->item('user_goodscode_img_dir').$data['GoodsCode'];
			//debug($goods_img_dir);

			$query = $this->db->query("SELECT id FROM goods_code WHERE gcode='{$data['GoodsCode']}'");
			if($query->num_rows() == 0)
			{
				$insert_data = array(
							   'gcode' => $data['GoodsCode'],
							   'created' => date('Y-m-d H:i:s', time())
							);

				$this->db->insert('goods_code', $insert_data);
			}

			if(!is_dir($goods_img_dir))
			{
				//debug($goods_img_dir);
				mkdir($goods_img_dir, 0755, TRUE);
			}
		}

		if($DataRtn) return $GdsMstId;
		else
		{
			if($GdsMstId > 0)
				echo '{"info":{"success":true,"text":"'.$GdsMstId.'"}}';
			else
				echo '{"info":{"success":false,"text":"상품 등록 오류(1)입니다."}}';
		}
	}

	// 상품 등록/수정/복사 처리
	function update_process($data)
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->model('goods_m');

		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		// 상품별 상세정보 테이블 GoodsInsertJson 필드 저장
		$post_json = json_encode($data);
		//debug($post_json);

		if(isset($data['GoodsExcel'])) $ErrorRtn = true;
		else $ErrorRtn = false;
		//debug($ErrorRtn);exit;

		// model_id 기본값 전처리 (int 컬럼 빈문자열 오류 방지)
		if (!isset($data['model_id']) || $data['model_id'] === '') $data['model_id'] = 0;

        // 입력된 매입처 확인 추가(2021.06.09)
        if(isset($data['GoodsEtc6']) && $data['GoodsEtc6'])
        {
            $GoodsEtc6 = addslashes($data['GoodsEtc6']);
			// 상품등록 가능 업체 확인, 다운승인 여부로 확인(2022.04.06)
			if($GoodsEtc6 == '모다') {
				echo '{"info":{"success":false,"text":"입력된 매입처 정보가 잘못되었습니다. 매입처명을 정확히 입력되었는지 확인해주세요."}}';
                exit;
			}
            $sql = "SELECT count(id) AS cnt FROM users WHERE auth_code = '4' AND username = '{$GoodsEtc6}'";
            // debug($sql);exit;
            $query = $this->db->query($sql);
            $row = $query->row();
            if($row->cnt < 1)
            {
                echo '{"info":{"success":false,"text":"입력된 매입처 정보가 없습니다. 매입처명을 정확히 입력되었는지 확인해주세요."}}';
                exit;
            }
        }

		// 상품 원가에 의한 자동 계산 처리
		if($data['GoodsEtc9'] > 0)
		{
			// 판매가 없을 때
			if(!$data['GoodsPrice']) $data['GoodsPrice'] = ceil(($data['GoodsEtc9'] * 2.3) / 100) * 100;	// 10단위 올림
			// TAG가 없을 때
			if(!$data['GoodsEtc32']) $data['GoodsEtc32'] = $data['GoodsEtc9'] * 4;	// 4배
			// 단하루판매가 없을 때
			if(!$data['GoodsEtc33']) $data['GoodsEtc33'] = ceil(($data['GoodsEtc9'] / 0.7) / 100) * 100;	// 10단위 올림
			// 도매꾹/오너클랜/도매창고 판매가 없을 때
			if(!$data['GoodsEtc34']) $data['GoodsEtc34'] = ceil(($data['GoodsEtc9'] / 0.6) / 100) * 100;	// 10단위 올림
			// 미미야/스토어팜 판매가 없을 때
			if(!$data['GoodsEtc35']) $data['GoodsEtc35'] = ceil(($data['GoodsEtc9'] / 0.5) / 100) * 100;	// 10단위 올림
		}

		// 상품코드 중복체크 및 상품코드별 이미지경로 자동생성
		if(isset($data['GoodsCode']) && $data['GoodsCode'])
		{
			/*
			// 상품코드 중복체크 추가(2019.01.31)
			//  - 상품코드 패턴 : BL2053K91 => BL(상품종류)2053(연번)K(국가)9(연도)1(월)
			//  - 뒤에 3자리 길이는 고정이라 나머지 코드로 중복 체크
			//  - 기존 상품 수정 시 2018.10.01 이전 등록된 상품꺼는 중복 체크 안함
			//  - 상품종류와 연번이 중복되면 안됩니다. BL2053은 1개의 코드만 존재해야합니다.
			//  - 이를 체크하고 중복시 “이미등록된 상품코드입니다.” 메시지 노출
			*/

			// 신규등록
			if($data['GoodsCmd'] == '1')
			{
				$query = $this->db->query("SELECT id FROM goods WHERE GoodsCode_1 = '{$data['GoodsCode_1']}' AND GoodsCode_2 = '{$data['GoodsCode_2']}'");
				if($query->num_rows() > 0)
				{
					echo '{"info":{"success":false,"text":"이미 등록된 상품코드입니다."}}';
					exit;
				}
			}
			else
			{
				$GoodsCodeCheckedNum = 1;
				// $query = $this->db->query("SELECT id FROM goods WHERE GoodsCode LIKE '{$data['GoodsCode_1']}%'");
				$query = $this->db->query("SELECT id FROM goods WHERE GoodsCode = '{$data['GoodsCode']}'");
				if($query->num_rows() > $GoodsCodeCheckedNum)
				{
					echo '{"info":{"success":false,"text":"이미 등록된 상품코드입니다."}}';
					exit;
				}
			}

			if(!$data['GoodsEtc60']) $data['GoodsEtc60'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_1.jpg';	// 대표이미지
			if(!$data['GoodsEtc61']) $data['GoodsEtc61'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_1.jpg';	// 종합몰jpg이미지
			if(!$data['GoodsEtc62']) $data['GoodsEtc62'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_2.jpg';	// 부가이미지2
			if(!$data['GoodsEtc63']) $data['GoodsEtc63'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_1.jpg';	// 부가이미지4
			if(!$data['GoodsEtc64']) $data['GoodsEtc64'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_2.jpg';	// 부가이미지5
			if(!$data['GoodsEtc65']) $data['GoodsEtc65'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_3.jpg';	// 부가이미지6
			if(!$data['GoodsEtc66']) $data['GoodsEtc66'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_4.jpg';	// 부가이미지7
			if(!$data['GoodsEtc67']) $data['GoodsEtc67'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_5.jpg';	// 부가이미지8
			if(!$data['GoodsEtc68']) $data['GoodsEtc68'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_6.jpg';	// 부가이미지9
			if(!$data['GoodsEtc69']) $data['GoodsEtc69'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-270.gif';	// 부가이미지11
			if(!$data['GoodsEtc70']) $data['GoodsEtc70'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-270.gif';	// 부가이미지12
			if(!$data['GoodsEtc74']) $data['GoodsEtc74'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-list2.jpg';	// 부가이미지15(2019.10.23 추가)
			if(!$data['GoodsEtc71']) $data['GoodsEtc71'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-600_1.jpg';	// 부가이미지20
			if(!$data['GoodsEtc72']) $data['GoodsEtc72'] = 'http://danharoo.negagea.kr/mimi/'.strtolower($data['GoodsCode']).'-220.jpg';	// 부가이미지22
		}

		// 배송비 기본값
		if(!$data['GoodsEtc55']) $data['GoodsEtc55'] = '2500';

		// 쇼핑몰별 상품명1 (50kb)-11번가,지마켓,옥션,인터파크 : 상품명+검색어 : 50kb 이하
        // 상품명으로 통일(2021.09.06) 변경
		if(!$data['GoodsEtc36'])
			$data['GoodsEtc36'] = str_cutting($data['GoodsName'], 51200);
			// $data['GoodsEtc36'] = str_cutting($data['GoodsName'].str_replace("#", ",", $data['GoodsEtc24']), 51200);
		else
			$data['GoodsEtc36'] = str_cutting($data['GoodsEtc36'], 51200);

		// 입점몰상품명 (100kb)-왕도매,도매창고 : 상품명+검색어 : 100kb 이하
		$GoodsNameArr = explode('-', $data['GoodsName']);
		// if(!$data['GoodsEtc37'])
		// {
		// 	//$data['GoodsEtc37'] = str_cutting(str_replace(strtolower($data['GoodsCode']).'-', "", strtolower($data['GoodsName'])).str_replace("#", " ", $data['GoodsEtc24']), 10000);
		// 	$data['GoodsEtc37'] = $GoodsNameArr[1].str_replace("#", " ", $data['GoodsEtc24']);
		// }
		// else
		// 	$data['GoodsEtc37'] = str_cutting($data['GoodsEtc37'], 51200);
		if(!$data['GoodsEtc37'])
			$data['GoodsEtc37'] = str_cutting($GoodsNameArr[1].str_replace("#", ",", $data['GoodsEtc24']), 51200);	// 상품명으로 변경(상품명+검색어)(2020.12.08)
		else
			$data['GoodsEtc37'] = str_cutting($data['GoodsEtc37'], 51200);

		// 쇼핑몰별 상품명3 - 스토어팜 : 상품코드 제외한 상품명 ex)OPS1188-플레어 코튼 롱 원피스 -> 플레어 코튼 롱 원피스
		// if(!$data['GoodsEtc38']) $data['GoodsEtc38'] = ($GoodsNameArr[1])?$GoodsNameArr[1]:'';
		if(!$data['GoodsEtc38']) $data['GoodsEtc38'] = $data['GoodsEtc37'];	// 쇼핑몰별 상품명1 동일반영(2020.12.08)

		// 카페24 상품명 :상품명+검색어 숨김태그 ex) 상품명<span class=displaynone>[검색어,검색어...]</span>
        // 상품명으로 통일(2021.09.06) 변경
		if(!$data['GoodsEtc39']) $data['GoodsEtc39'] = $data['GoodsName'];
		// if(!$data['GoodsEtc39']) $data['GoodsEtc39'] = $data['GoodsName'].'<span class=displaynone>['.str_replace("#", ",", substr_replace($data['GoodsEtc24'], "", 0, 1)).']</span>';

		// 상품상세 설명 자동체움 데이타 반영(2021.04.09)
		$Description_txt = '컬러는 '.$data['OptionColor'].' 총 '.count(explode(',', $data['OptionColor'])).'가지 컬러로 준비했구요'.PHP_EOL.PHP_EOL;
		$Description_txt .= '사이즈는 '.$data['OptionSize'].' 사이즈로'.PHP_EOL;
		$Description_txt .= '44~66사이즈 언니들에게 추천해 드려요! MD.';


		$data_txt = '';
		if($data['GoodsName'])
		{
			$data_txt .= '★ 상호 : 단하루[www.danharoo.com]'.PHP_EOL.PHP_EOL;
			$data_txt .= '★ 상품명 : '.$data['GoodsName'].PHP_EOL.PHP_EOL;
			$data_txt .= $data['GoodsEtc24'].PHP_EOL;
			$data_txt .= '------------------------------------------------------------------------------------------'.PHP_EOL;
			$data_txt .= ' - FABRIC(소재) : '.$data['GoodsEtc16'].PHP_EOL.PHP_EOL;
			$data_txt .= ' - COLOR(색상) : '.$data['OptionColor'].PHP_EOL.PHP_EOL;
			$data_txt .= ' - SIZE(사이즈) : '.$data['OptionSize'].PHP_EOL.PHP_EOL;
			$data_txt .= ' - 상세사이즈 : '.PHP_EOL.PHP_EOL;
			$data_txt .= $data['GoodsEtc14'].PHP_EOL.PHP_EOL;
			$data_txt .= '------------------------------------------------------------------------------------------'.PHP_EOL;
			$data_txt .= '★ 오후 3시이전 주문 당일 출고'.PHP_EOL.PHP_EOL;
			$data_txt .= '★ 이미지 사용 불가입니다.[유료로 상세 사진은 단하루 사이트 이용 부탁드립니다.]'.PHP_EOL;
			$data_txt .= '★ 온라인 업체 위탁 배송 가능합니다.'.PHP_EOL.PHP_EOL;
			$data_txt .= '★ 기본 택배비 2500원'.PHP_EOL;
			$data_txt .= '★ 모든 상품은 불량교환만 가능 합니다.'.PHP_EOL;
			$data_txt .= '------------------------------------------------------------------------------------------'.PHP_EOL;
			$data_txt .= '★ 주문 카카오톡: 플러스친구 "단하루" /  010-3442-0511'.PHP_EOL.PHP_EOL;
			$data_txt .= '☎ 전화: 070-4333-0422 [ 오전10시~오후6시 ]'.PHP_EOL.PHP_EOL;
			$data_txt .= '★ 계좌번호 : 기업 267-049642-01-011 오병용'.PHP_EOL.PHP_EOL;
			$data_txt .= '========================================================================================='.PHP_EOL;
		}

		if(!$data['DanharooGoodsName']) $data['DanharooGoodsName'] = $data['GoodsName'];

		$data['GoodsEtc58'] = $data_txt;	// 신상마켓 상세설명
		$data['GoodsEtc59'] = $data_txt;	// 카카오스토리 상세설명

		//: 쇼셜용 옵션 확인
		if(!$data['SocialGoodsOption']) $data['SocialGoodsOption'] = $data['OptionSize'].'/'.$data['OptionColor'];

		/************** 단하루상품설명 자동반영(2019.02.28 추가) html 내용 수정(2020.12.04)  ***************/
		$goods_info_html_data = $this->config->item('goods_info_html_data');
		// if($data['GoodsDetailSave'] == 'Y') $goods_info_html_data = $this->config->item('goods_info_html_data_new');	// html 내용 수정(2020.12.04) 반영
		// 상세정렬 기능에서 자동반영으로 주석처리(2021.01.05)

        // 이미지경로 외부도메인 반영(2018.02.20)
        if($data['GoodsEtc73'])
            $goods_info_html_data = str_replace("{GoodsImgPath}", $data['GoodsEtc73'], $goods_info_html_data);
        else
            $goods_info_html_data = str_replace("{GoodsImgPath}", "http://newtalk.kr/data/files/goods/img/".$data['GoodsCode']."/", $goods_info_html_data);

		$goods_info_html_data = str_replace("{GoodsCode}", strtolower($data['GoodsCode']), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsName}", $data['DanharooGoodsName'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{Description}", nl2br($data['Description']), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc24}", '['.$data['GoodsEtc24'].']', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc25}", $data['GoodsEtc25'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc26}", $data['GoodsEtc26'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc16}", $data['GoodsEtc16'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionColor}", $data['OptionColor'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc15}", $data['GoodsEtc15'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc13}", ',사이즈('.$data['GoodsEtc13'].')', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc18}", $data['GoodsEtc18'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc17}", $data['GoodsEtc17'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionSize}", $data['OptionSize'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc14}", nl2br($data['GoodsEtc14']), $goods_info_html_data);
		$goods_info_html_data = str_replace("{MakerName}", $data['MakerName'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc20}", $data['GoodsEtc20'], $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc21}", $data['GoodsEtc21'], $goods_info_html_data);

		$DanharooDescription = $goods_info_html_data;	// 단하루상품설명
		/************** 단하루상품설명 자동반영(2019.02.28 추가)  ***************/

        // 추가상품상세설명1 자동반영(2021.09.06)
        // 1. 사이즈가 FREE인경우 : FREE(fit사이즈감항목)
        // 2. 사이즈가 있는 경우 : fit사이즈감항목
        if($data['OptionSize'])
        {
            // 사이즈가 FREE인경우
            if($data['OptionSize'] == 'FREE') {
                $data['GoodsEtc27'] = 'FREE('.$data['GoodsEtc13'].')';
            } else {
                $data['GoodsEtc27'] = $data['GoodsEtc13'];
            }
        }

		// 신규등록
		if($data['GoodsCmd'] == '1')
		{
			//debug($data);exit;
			$data_gb_txt = '상품등록';

			$GoodsImageArr = explode('||', $data['GoodsImageList']);

			// 참고용이미지 추가(2020.12.13)
			$GoodsImageEtcArr = explode('||', $data['GoodsImageEtcList']);

			// ESMPLUS 옥션과 지마켓 둘 다 보낼 때 통합으로 한 번만 보내기 위한 설정
			$ESMPLUS_ALL = FALSE;
			//if(in_array('A', $data['SellType']) && in_array('B', $data['SellType'])) $ESMPLUS_ALL = TRUE;
			//debug($ESMPLUS_ALL);exit;

			// 등록 마켓별 상품등록
			//$SellingPeriodArr = explode("|", $data['SellingPeriod']);

			$insert_data = array(
							'user_id'					=> $user_id,
							'GdsMstId'					=> $data['GdsMstId'],
							'market'					=> 'H',
							'Category1'					=> $data['Category1'],
							'Category2'					=> $data['Category2'],
							'Category3'					=> $data['Category3'],
							'GoodsName'					=> $data['GoodsName'],
							'DanharooGoodsName'			=> $data['DanharooGoodsName'],	// 단하루상품명 추가(2019.02.28)
							'GoodsCode_1'				=> $data['GoodsCode_1'],		// 상품코드 자동생성 구분 추가(2020.08.08)
							'GoodsCode_2'				=> $data['GoodsCode_2'],		// 상품코드 자동생성 구분 추가(2020.08.08)
							'GoodsCode_3'				=> $data['GoodsCode_3'],		// 상품코드 자동생성 구분 추가(2020.08.08)
							'GoodsCode_4'				=> $data['GoodsCode_4'],		// 상품코드 자동생성 구분 추가(2020.08.08)
							'GoodsCode_5'				=> $data['GoodsCode_5'],		// 상품코드 자동생성 구분 추가(2020.08.08)
							'GoodsCode_6'				=> $data['GoodsCode_6'],		// 상품코드 자동생성 구분 추가(2021.07.01)
							'GoodsCode'					=> $data['GoodsCode'],			// 상품코드 추가(2017.02.09)
							'CatalogName'				=> '',
							'BrandName'					=> $data['BrandName'],   // 브랜드명 추가(2020.05.26)
							'MakerName'					=> $data['MakerName'],
							'SellingPeriod'				=> '',
							'SellingPeriodStart'		=> '',
							'SellingPeriodEnd'			=> '',
							'GoodsPrice'				=> $data['GoodsPrice'],
							'GoodsCount'				=> $data['GoodsCount'],
							'GoodsOptionsUseSetting'	=> '',
							'GoodsImage'				=> $GoodsImageArr[0],
							'CommonDeliveryWayOPTSEL'	=> '',
							'DeliveryCOMP'				=> '',
							'ShipmentPlaceNo'			=> '',
							'DeliveryFeeType'			=> '',
							'NoticeItemGroupNo'			=> '',
							'OptionColor'				=> $data['OptionColor'],
							'OptionSize'				=> $data['OptionSize'],
							'OptionColorChina'			=> $data['OptionColorChina'],	// 셀메이트 옵션 추가(2019.12.15)
							'OptionSizeChina'			=> $data['OptionSizeChina'],	// 셀메이트 옵션 추가(2019.12.15)
							'SocialGoodsOption'			=> $data['SocialGoodsOption'],	// 쇼셜용 옵션 추가(2018.05.30)

							'GoodsEtc4'			=> $data['GoodsEtc4'],
							'GoodsEtc5'			=> $data['GoodsEtc5'],
							'GoodsEtc6'			=> $data['GoodsEtc6'],
							'GoodsEtc7'			=> $data['GoodsEtc7'],
							'GoodsEtc8'			=> $data['GoodsEtc8'],
							'GoodsEtc9'			=> $data['GoodsEtc9'],
							'GoodsEtc10'		=> $data['GoodsEtc10'],
							'GoodsEtc13'		=> $data['GoodsEtc13'],
							'GoodsEtc16'		=> $data['GoodsEtc16'],
							'GoodsEtc17'		=> $data['GoodsEtc17'],
							'GoodsEtc18'		=> $data['GoodsEtc18'],
							'GoodsEtc20'		=> $data['GoodsEtc20'],
							'GoodsEtc21'		=> $data['GoodsEtc21'],
							'GoodsEtc24'		=> $data['GoodsEtc24'],
							'GoodsEtc32'		=> $data['GoodsEtc32'],
							'GoodsEtc33'		=> $data['GoodsEtc33'],
							'GoodsEtc34'		=> $data['GoodsEtc34'],
							'GoodsEtc35'		=> $data['GoodsEtc35'],
							'GoodsEtc36'		=> $data['GoodsEtc36'],
							'GoodsEtc37'		=> $data['GoodsEtc37'],
							'GoodsEtc38'		=> $data['GoodsEtc38'],
							'GoodsEtc39'		=> $data['GoodsEtc39'],
                            'GoodsEtc40'		=> $data['GoodsEtc40'],   	// 뉴톡 상품명 추가(2017.11.09)
                            'GoodsEtc41'		=> $data['GoodsEtc41'],   	// 뉴톡 판매가 추가(2017.11.09)
							'GoodsEtc48'		=> $data['GoodsEtc48'],
							'GoodsEtc51'		=> (is_numeric($data['GoodsEtc51']) ? $data['GoodsEtc51'] : ''),
							'GoodsEtc52'		=> $data['GoodsEtc52'],
							'GoodsEtc53'		=> $data['GoodsEtc53'],
							'GoodsEtc54'		=> $data['GoodsEtc54'],
							'GoodsEtc55'		=> $data['GoodsEtc55'],
							'GoodsEtc56'		=> $data['GoodsEtc56'],
							'GoodsEtc57'		=> $data['GoodsEtc57'],
                            'GoodsOnly'			=> $data['GoodsOnly'],   	// 단독상품여부 추가(2020.05.26)
                            'model_id'			=> ($data['model_id'] !== '' ? intval($data['model_id']) : 0),	//촬영모델(user_model.id, 2025.12.15 추가)
                            'BizProgress'		=> 'A1',   					// 상품업무진행상태 디폴트[입고(A1)] 반영(2020.08.31)
                            'BizProgressUpdate'	=> $this->info['created'],  // 상품업무진행상태 수정일 반영(2020.08.31)
							'activated'			=> $data['activated'],
							'mall_activated'	=> $data['mall_activated'],	// 상품미니몰노출여부 추가(2019.02.18)
							're_created'		=> $this->info['created'],  // 재등록일 추가(2021.10.21)
							'created'			=> $this->info['created']
						);

			// 단독상품옵션 수정일 추가(2020.05.26) - 신규등록일 때는 'Y'일 때만 수정일 및 로그 반영
			if($data['GoodsOnly'] == 'Y') {
				$insert_data['GoodsOnlyDay'] = $this->info['today'];
			}

			$this->db->insert('goods', $insert_data);
			$goods_id = $this->db->insert_id();

            // 상품별 옵션자체코드 자동생성(2022.05.03) 추가
            if($goods_id && $data['OptionSize'] && $data['OptionColor'])
                $this->size_color_code_make($goods_id, $data['OptionSize'], $data['OptionColor']);

			// 상품별 상세정보 테이블
			$insert_data2 = array(
							'goods_id'			=> $goods_id,
							'GoodsOptVal'		=> $data['GoodsOptVal'],
							'Description'		=> ($data['Description'])?$data['Description']:$Description_txt,
							'NoticeItemCodes'	=> '',
							'DanharooDescription'	=> $DanharooDescription,	// 단하루상품설명 추가(2019.02.28)
							'GoodsInsertJson'	=> $post_json,

							'GoodsEtc14'		=> $data['GoodsEtc14'],
							'GoodsEtc15'		=> $data['GoodsEtc15'],
							'GoodsEtc22'		=> $data['GoodsEtc22'],
							'GoodsEtc25'		=> $data['GoodsEtc25'],
							'GoodsEtc26'		=> $data['GoodsEtc26'],
							'GoodsEtc27'		=> $data['GoodsEtc27'],
							'GoodsEtc28'		=> $data['GoodsEtc28'],
							'GoodsEtc29'		=> $data['GoodsEtc29'],
							'GoodsEtc30'		=> $data['GoodsEtc30'],
							'GoodsEtc58'		=> $data['GoodsEtc58'],
							'GoodsEtc59'		=> $data['GoodsEtc59'],

							'GoodsEtc60'		=> $data['GoodsEtc60'],
							'GoodsEtc61'		=> $data['GoodsEtc61'],
							'GoodsEtc62'		=> $data['GoodsEtc62'],
							'GoodsEtc63'		=> $data['GoodsEtc63'],
							'GoodsEtc64'		=> $data['GoodsEtc64'],
							'GoodsEtc65'		=> $data['GoodsEtc65'],
							'GoodsEtc66'		=> $data['GoodsEtc66'],
							'GoodsEtc67'		=> $data['GoodsEtc67'],
							'GoodsEtc68'		=> $data['GoodsEtc68'],
							'GoodsEtc69'		=> $data['GoodsEtc69'],
							'GoodsEtc70'		=> $data['GoodsEtc70'],
							'GoodsEtc71'		=> $data['GoodsEtc71'],
							'GoodsEtc72'		=> $data['GoodsEtc72'],
                            'GoodsEtc73'		=> $data['GoodsEtc73'],
                            'GoodsEtc74'		=> $data['GoodsEtc74'],	// 부가이미지15 추가(2019.10.23)
							'GoodsMovieUrl'		=> $data['GoodsMovieUrl'],	// 상품동영상주소 추가(2020.09.25)
							'GoodsEtcSerializes'	=> $data['GoodsEtcSerializes']	// 상품별 기타정보 추가(2020.11.12)
						);

			// 코디상품코드 반영(2021.01.14)
			if($data['CoordiGoodsCodes'])
            {
                $CoordiGoodsCodesArr = explode(',', $data['CoordiGoodsCodes']);
                // debug($CoordiGoodsCodesArr);

                // 해당 코디상품에 해당 상품코드 추가(2021.08.24)
                foreach($CoordiGoodsCodesArr AS $code)
                {
                    // 추가되는 코디상품에 해당 상품코드가 적용되어 있는지 확인
                    $sql = "SELECT
                                GS.id, GSD.CoordiGoodsCodes
                                FROM
                                    goods AS		GS LEFT OUTER JOIN
                                    goods_detail	As GSD ON GS.id = GSD.goods_id
                                WHERE
                                    GS.GoodsCode='{$code}'
                    ";
                    $query = $this->db->query($sql);
                    $row = $query->row();
                    $CoordiGoodsId = $row->id;
                    $CoordiGoodsCodes = $row->CoordiGoodsCodes;

                    // 없는 상품코드이면 추가
                    $CoordiGoodsCodes .= $CoordiGoodsCodes ? ','.$data['GoodsCode'] : $data['GoodsCode'];

                    $update_data = array(
                                    'CoordiGoodsCodes'	=> $CoordiGoodsCodes
                                );
                    $this->db->where('goods_id', $CoordiGoodsId);
                    $this->db->update('goods_detail', $update_data);
                }

                $insert_data2['CoordiGoodsCodes'] = $data['CoordiGoodsCodes'];
            }

			$this->db->insert('goods_detail', $insert_data2);

			// 상품별 이미지정보 테이블
			$insert_data3 = array(
						   'goods_id'			=> $goods_id
						);

			foreach($GoodsImageArr AS $k => $v)
			{
				if($k < 20) $insert_data3['img'.$k] = $v;
			}
			// 참고용이미지 추가(2020.12.13)
			foreach($GoodsImageEtcArr AS $k => $v)
			{
				if($k < 10) $insert_data3['img_etc'.$k] = $v;
			}
			$this->db->insert('goods_image', $insert_data3);

            // 도매업체 입점상품관리에 따른 매입처명별 상품 추가 데이타 반영(2021.11.03)
            // 매입처명 확인 후 계약 총 상품수 / 추가 상품금액 확인
            $data['created'] = $insert_data['created'];
            $this->wholesalebiz_entered_goods($goods_id, $data);

			// 단독상품옵션 수정일 추가(2020.05.26) - 신규등록일 때는 'Y'일 때만 수정일 및 로그 반영
			if($data['GoodsOnly'] == 'Y') {
				// 단독상품옵션 변경 로그 데이타
				$insert_data4 = array(
					'before'	=> 'N',
					'after'		=> $data['GoodsOnly'],
					'user_id'	=> $user_id,
					'goods_id'	=> $goods_id,
					'user_ip'	=> $this->input->ip_address()
				);
				$this->db->insert('goods_only_logs', $insert_data4);
			}

			// 상품 액션(등록) 로그
			$this->goods_m->action_logs($this->session->userdata('user_id'), $goods_id, 'A');

			// 상품 업무진행상태 로그 - 등록시 '입고(A1)' 자동처리 반영(2020.08.31)
			$this->goods_m->biz_logs($this->session->userdata('user_id'), $goods_id, 'A1', $this->info['created']);

			/* // 송출 비활성 시킴(cron 으로 인한 자동 송출을 위해)
			if($goods_id > 0)
			{
				//debug($goods_id);
				// 마켓별 소켓 등록

				// ESMPLUS 옥션과 지마켓 둘 다 보낼 때 통합으로 한 번만 보내기 위한 설정
				if( ($market == 'A' || $market == 'B') && $ESMPLUS_ALL == TRUE)
					$this->market_update($goods_id, '', '', TRUE);
				else
					$this->market_update($goods_id);
			}
			else
			{
				if($ErrorRtn) return '{"info":{"success":false,"error":{"kind":"db","key":"1","msg":"디비 등록 에러입니다."}}}';
				echo '{"info":{"success":false,"text":"'.$data_gb_txt.' 오류(1)입니다."}}';
				exit;
			}
			*/

			//delete_files($this->tmp_upload_url, true);
			//@rmdir($this->tmp_upload_url);

			if($ErrorRtn) return '{"info":{"success":true,"text":"'.$data_gb_txt.' 완료입니다."}}';
			echo '{"info":{"success":true,"text":"'.$data_gb_txt.' 완료입니다."}}';
		}
		// 수정
		else if($data['GoodsCmd'] == '2')
		{
			// debug($data);
			$data_gb_txt = '상품수정';

			// 수정 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.Description, GSD.NoticeItemCodes, GSD.MarketInsertJson,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods AS		GS LEFT OUTER JOIN
							goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image		As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$data['GoodsId']}'
			";
			$query = $this->db->query($sql);
			$row = $query->row();
			//debug($row);

			// 상품번호가 없으면 수정불가
			/*
			if(!$row->GoodsNo || $row->GoodsNo == '0')
			{
				echo '{"info":{"success":false,"text":"상품번호가 없는 상품은 수정이 불가합니다."}}';
				exit;
			}
			*/

			// 회원 상품이 맞는지 확인
			/*
			if($row->user_id != $user_id)
			{
				echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
				exit;
			}
			*/

			$data['market'] = $row->market;
			$data['GoodsNo'] = $row->GoodsNo;
			//$data['MarketInsertJson'] = $row->MarketInsertJson;

			//$data['AfterDays'] = ($data['AfterDays'])?$data['AfterDays']:'';
			//$data['StyleW'] = ($data['StyleW'])?$data['StyleW']:'';

			// debug($data);exit;

			// 상품 이미지 확인 및 등록
			if($data['GoodsImageList'])
			{
				$data['GoodsImageCheck'] = array(); // 이미지명 등록 체크
				$GoodsImageArr = explode('||', $data['GoodsImageList']);
				//debug($GoodsImageArr);
				$tmp_file_arr = get_filenames($this->tmp_upload_url); // 임시 이미지 배열
				//debug($tmp_file_arr);

				$goods_image_update_data = array(); // 이미지 정보 테이블 업데이트 필드셋
				foreach($GoodsImageArr AS $k => $v)
				{
					if($v)
					{
						// 이미지명이 임시경로에 있는지 확인(in_array()가 대소문자를 구분), 있으면 신규 이미지등록
						if( in_array($v, $tmp_file_arr) )
						{
							$data['GoodsImageCheck'][$v] = 'Y'; // 신규 이미지 체크

							// 메인이미지만 썸네일 저장
							if($k == 0)
							{
								$config['source_image']	= $this->tmp_upload_url.'/'.$v;
								$config['new_image']	= $this->tmp_upload_url.'/thumbnail/';
								if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

								if(!is_file($config['new_image'].$v))
								{
									// thumbnail 생성
									$this->load->library('image_lib');
									$config['image_library'] = 'gd2';
									$config['maintain_ratio'] = TRUE;
									$config['width']	= 100;
									$config['height']	= 100;
									//debug($config);
									$this->image_lib->initialize($config);
									$this->image_lib->resize();
								}
							}
						}
						else
							$data['GoodsImageCheck'][$v] = 'N';

						$goods_image_update_data['img'.$k] = $v;
					}
				}
			}

			// 참고용이미지 추가(2020.12.13)
			if($data['GoodsImageEtcList'])
			{
				$data['GoodsImageEtcCheck'] = array(); // 이미지명 등록 체크
				$GoodsImageEtcArr = explode('||', $data['GoodsImageEtcList']);
				//debug($GoodsImageArr);
				$tmp_file_arr = get_filenames($this->tmp_upload_url); // 임시 이미지 배열
				//debug($tmp_file_arr);

				foreach($GoodsImageEtcArr AS $k => $v)
				{
					if($v)
					{
						// 이미지명이 임시경로에 있는지 확인(in_array()가 대소문자를 구분), 있으면 신규 이미지등록
						if( in_array($v, $tmp_file_arr) )
						{
							$data['GoodsImageEtcCheck'][$v] = 'Y'; // 신규 이미지 체크

							// 메인이미지만 썸네일 저장
							if($k == 0)
							{
								$config['source_image']	= $this->tmp_upload_url.'/'.$v;
								$config['new_image']	= $this->tmp_upload_url.'/thumbnail/';
								if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

								if(!is_file($config['new_image'].$v))
								{
									// thumbnail 생성
									$this->load->library('image_lib');
									$config['image_library'] = 'gd2';
									$config['maintain_ratio'] = TRUE;
									$config['width']	= 100;
									$config['height']	= 100;
									//debug($config);
									$this->image_lib->initialize($config);
									$this->image_lib->resize();
								}
							}
						}
						else
							$data['GoodsImageEtcCheck'][$v] = 'N';

						$goods_image_update_data['img_etc'.$k] = $v;
					}
				}
			}

			// debug($goods_image_update_data);exit;

			// 상품 goods 테이블 수정
			$goods_data1 = array(
				'Category1'			=> $data['Category1'],
				'Category2'			=> $data['Category2'],
				'Category3'			=> $data['Category3'],
				'GoodsName'			=> $data['GoodsName'],
				'DanharooGoodsName'	=> $data['DanharooGoodsName'],	// 단하루상품명 추가(2019.02.28)
				'GoodsCode_6'		=> $data['GoodsCode_6'],		// 상품코드 자동생성 구분 추가(2021.07.02)
				'GoodsCode'			=> $data['GoodsCode'],
				'BrandName'			=> $data['BrandName'],   // 브랜드명 추가(2020.05.26)
				'MakerName'			=> $data['MakerName'],
				'GoodsPrice'		=> $data['GoodsPrice'],
				'GoodsCount'		=> ($data['GoodsCount'] !== '' ? $data['GoodsCount'] : 0),
				'OptionColor'		=> $data['OptionColor'],
				'OptionSize'		=> $data['OptionSize'],
				'OptionColorChina'	=> $data['OptionColorChina'],	// 셀메이트 옵션 추가(2019.12.15)
				'OptionSizeChina'	=> $data['OptionSizeChina'],	// 셀메이트 옵션 추가(2019.12.15)
				'SocialGoodsOption'	=> $data['SocialGoodsOption'],	// 쇼셜용 옵션 추가(2018.05.30)
				'GoodsEtc4'			=> $data['GoodsEtc4'],
				'GoodsEtc5'			=> $data['GoodsEtc5'],
				'GoodsEtc6'			=> $data['GoodsEtc6'],
				'GoodsEtc7'			=> $data['GoodsEtc7'],
				'GoodsEtc8'			=> $data['GoodsEtc8'],
				'GoodsEtc9'			=> $data['GoodsEtc9'],
				'GoodsEtc10'		=> $data['GoodsEtc10'],
				'GoodsEtc13'		=> $data['GoodsEtc13'],
				'GoodsEtc16'		=> $data['GoodsEtc16'],
				'GoodsEtc17'		=> $data['GoodsEtc17'],
				'GoodsEtc18'		=> $data['GoodsEtc18'],
				'GoodsEtc20'		=> $data['GoodsEtc20'],
				'GoodsEtc21'		=> $data['GoodsEtc21'],
				'GoodsEtc24'		=> $data['GoodsEtc24'],
				'GoodsEtc32'		=> $data['GoodsEtc32'],
				'GoodsEtc33'		=> $data['GoodsEtc33'],
				'GoodsEtc34'		=> $data['GoodsEtc34'],
				'GoodsEtc35'		=> $data['GoodsEtc35'],
				'GoodsEtc36'		=> $data['GoodsEtc36'],
				'GoodsEtc37'		=> $data['GoodsEtc37'],
				'GoodsEtc38'		=> $data['GoodsEtc38'],
				'GoodsEtc39'		=> $data['GoodsEtc39'],
                'GoodsEtc40'		=> $data['GoodsEtc40'],		// 뉴톡 상품명 추가(2017.11.09)
                'GoodsEtc41'		=> $data['GoodsEtc41'],		// 뉴톡 판매가 추가(2017.11.09)
				'GoodsEtc48'		=> $data['GoodsEtc48'],
				'GoodsEtc51'		=> (is_numeric($data['GoodsEtc51']) ? $data['GoodsEtc51'] : ''),
				'GoodsEtc52'		=> $data['GoodsEtc52'],
				'GoodsEtc53'		=> $data['GoodsEtc53'],
				'GoodsEtc54'		=> $data['GoodsEtc54'],
				'GoodsEtc55'		=> $data['GoodsEtc55'],
				'GoodsEtc56'		=> $data['GoodsEtc56'],
				'GoodsEtc57'		=> $data['GoodsEtc57'],
				'GoodsOnly'			=> $data['GoodsOnly'],   // 단독상품여부 추가(2020.05.26)
				'model_id'			=> ($data['model_id'] !== '' ? intval($data['model_id']) : 0),	//촬영모델(user_model.id, 2025.12.15 추가)
				'activated'			=> $data['activated'],
				'mall_activated'	=> $data['mall_activated']	// 상품미니몰노출여부 추가(2019.02.18)
			);

            // 상품별 옵션자체코드 자동생성(2022.05.03) 추가
            if($data['GoodsId'] && $data['OptionSize'] && $data['OptionColor'])
                $this->size_color_code_make($data['GoodsId'], $data['OptionSize'], $data['OptionColor']);

            // 엑셀 수정 미반영으로 인해 별도 반영(2021.05.26)
            if(!isset($data['GoodsExcel'])) {
                $goods_data1['GoodsImage'] = $GoodsImageArr[0];
            }

			// 단독상품옵션 수정일 추가(2020.05.26) - 신규등록일 때는 'Y'일 때만 수정일 및 로그 반영
			if($data['GoodsOnly'] !== $row->GoodsOnly) {
				$goods_data1['GoodsOnlyDay'] = $this->info['today'];

				// 단독상품옵션 변경 로그 데이타
				$insert_data3 = array(
					'before'	=> $row->GoodsOnly,
					'after'		=> $data['GoodsOnly'],
					'user_id'	=> $user_id,
					'goods_id'	=> $data['GoodsId'],
					'user_ip'	=> $this->input->ip_address()
				);
				$this->db->insert('goods_only_logs', $insert_data3);
			}

			//debug($data['GoodsId']);
			$this->db->where('id', $data['GoodsId']);
			$this->db->update('goods', $goods_data1);
			//debug($this->db->last_query());
			/*
			if($this->db->affected_rows() < 1)
			{
				echo '{"info":{"success":false,"text":"디비 오류(1)입니다."}}';
				exit;
			}
			*/

			// 상품별 상세정보 테이블
			$goods_data2 = array(
				'Description'	=> ($data['Description'])?$data['Description']:$Description_txt,
				// 'DanharooDescription'	=> $DanharooDescription,	// 단하루상품설명 추가(2019.02.28)

				// 'GoodsEtc14'	=> $data['GoodsEtc14'],
				// 'GoodsEtc15'	=> $data['GoodsEtc15'],
				// 'GoodsEtcSerializes'	=> $data['GoodsEtcSerializes']	// 상품별 기타정보 추가(2020.11.14)

				'GoodsEtc22'	=> $data['GoodsEtc22'],
				'GoodsEtc25'	=> $data['GoodsEtc25'],
				'GoodsEtc26'	=> $data['GoodsEtc26'],
				'GoodsEtc27'	=> $data['GoodsEtc27'],
				'GoodsEtc28'	=> $data['GoodsEtc28'],
				'GoodsEtc29'	=> $data['GoodsEtc29'],
				'GoodsEtc30'	=> $data['GoodsEtc30'],
				'GoodsEtc58'	=> $data['GoodsEtc58'],
				'GoodsEtc59'	=> $data['GoodsEtc59'],
				'GoodsEtc60'	=> $data['GoodsEtc60'],
				'GoodsEtc61'	=> $data['GoodsEtc61'],
				'GoodsEtc62'	=> $data['GoodsEtc62'],
				'GoodsEtc63'	=> $data['GoodsEtc63'],
				'GoodsEtc64'	=> $data['GoodsEtc64'],
				'GoodsEtc65'	=> $data['GoodsEtc65'],
				'GoodsEtc66'	=> $data['GoodsEtc66'],
				'GoodsEtc67'	=> $data['GoodsEtc67'],
				'GoodsEtc68'	=> $data['GoodsEtc68'],
				'GoodsEtc69'	=> $data['GoodsEtc69'],
				'GoodsEtc70'	=> $data['GoodsEtc70'],
				'GoodsEtc71'	=> $data['GoodsEtc71'],
				'GoodsEtc72'	=> $data['GoodsEtc72'],
                'GoodsEtc73'	=> $data['GoodsEtc73'],
				'GoodsEtc74'	=> $data['GoodsEtc74'],	// 부가이미지15 추가(2019.10.23)
				'GoodsMovieUrl'	=> $data['GoodsMovieUrl']	// 상품동영상주소 추가(2020.09.25)
			);

            // 엑셀 수정 미반영으로 인해 별도 반영(2021.05.26)
            if(!isset($data['GoodsExcel'])) {
                $goods_data2['GoodsEtc14'] = $data['GoodsEtc14'];
                $goods_data2['GoodsEtc15'] = $data['GoodsEtc15'];
                $goods_data2['GoodsEtcSerializes'] = $data['GoodsEtcSerializes'];
            }

			// 상세정렬 기능에서 자동 반영되지 않는 상품만 반영(2021.01.05)
            // 엑셀수정등록시 미반영 처리(2021.06.02) 조건 추가
			if(!isset($data['GoodsExcel']) && $data['GoodsDetailSave'] != 'Y') $goods_data2['DanharooDescription'] = $DanharooDescription;

			// 코디상품코드 반영(2021.01.14)
			if(isset($data['CoordiGoodsCodes']))
            {
                $CoordiGoodsCodesArr = explode(',', $data['CoordiGoodsCodes']);
                // debug($CoordiGoodsCodesArr);

                // 해당 코디상품에 해당 상품코드 추가(2021.08.24)
                foreach($CoordiGoodsCodesArr AS $code)
                {
                    // 추가되는 코디상품에 해당 상품코드가 적용되어 있는지 확인
                    $sql = "SELECT
                                GS.id, GSD.CoordiGoodsCodes
                                FROM
                                    goods AS		GS LEFT OUTER JOIN
                                    goods_detail	As GSD ON GS.id = GSD.goods_id
                                WHERE
                                    GS.GoodsCode='{$code}'
                    ";
                    $query = $this->db->query($sql);
                    $row = $query->row();
                    $CoordiGoodsId = $row->id;
                    $CoordiGoodsCodes = $row->CoordiGoodsCodes;

                    $CoordiGoodsCodesArr2 = explode(',', $CoordiGoodsCodes);
                    $key = array_search(strtolower($code), $CoordiGoodsCodesArr);

                    // 해당 코드가 없으면 코디상품에 해당 상품코드 반영
                    if($key === false)
                    {
                        // 없는 상품코드이면 추가
                        $CoordiGoodsCodes .= $CoordiGoodsCodes ? ','.$data['GoodsCode'] : $data['GoodsCode'];

                        $update_data = array(
                                        'CoordiGoodsCodes'	=> $CoordiGoodsCodes
                                    );
                        $this->db->where('goods_id', $CoordiGoodsId);
                        $this->db->update('goods_detail', $update_data);
                    }
                }

                $goods_data2['CoordiGoodsCodes'] = $data['CoordiGoodsCodes'];
            }

			$this->db->where('goods_id', $data['GoodsId']);
			$this->db->update('goods_detail', $goods_data2);

			// 상품 goods_image 테이블 수정
            // 엑셀 수정 미반영으로 인해 해당 변수가 있을때 만 반영(2021.05.26)
            if(!isset($data['GoodsExcel']))
            {
                $goods_image_data = [];
                $img_where = "goods_id = ".$data['GoodsId'];
                for ($i=0; $i < 20; $i++)
                {
                    if(!isset($goods_image_update_data['img'.$i]) || $goods_image_update_data['img'.$i] == '')
                        $goods_image_data['img'.$i] = '';
                    else
                        $goods_image_data['img'.$i] = $goods_image_update_data['img'.$i];

                    if($i < 10)
                    {
                        if(!isset($goods_image_update_data['img_etc'.$i]) || $goods_image_update_data['img_etc'.$i] == '')
                            $goods_image_data['img_etc'.$i] = '';
                        else
                            $goods_image_data['img_etc'.$i] = $goods_image_update_data['img_etc'.$i];
                    }
                }
                $img_sql = $this->db->update_string('goods_image', $goods_image_data, $img_where);
                // debug($img_sql);exit;
                $this->db->query($img_sql);
            }

            // 도매업체 입점상품관리에 따른 매입처명별 상품 추가 데이타 반영(2021.11.03)
            // 매입처명 확인 후 계약 총 상품수 / 추가 상품금액 확인
            $data['created'] = $row->created;
            $this->wholesalebiz_entered_goods($data['GoodsId'], $data);

			// 상품 액션(수정) 로그
			$this->goods_m->action_logs($this->session->userdata('user_id'), $data['GoodsId'], 'B');

			//debug($data);

			// 마켓별 소켓 수정
			//$this->market_update($data['GoodsId'], 'UP', $data);

			//delete_files($this->tmp_upload_url, true);
			//@rmdir($files_url);
			if($ErrorRtn) return '{"info":{"success":true,"text":"'.$data_gb_txt.' 완료입니다."}}';
			echo '{"info":{"success":true,"text":"'.$data_gb_txt.' 완료입니다."}}';
		}
		// 복사
		else if($data['GoodsCmd'] == '3')
		{
			$data_gb_txt = '상품복사';

			// 복사 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.GoodsOptVal, GSD.Description, GSD.NoticeItemCodes, GSD.MarketInsertJson,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods AS		GS LEFT OUTER JOIN
							goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image		As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$data['GoodsId']}'
			";
			$query = $this->db->query($sql);
			$row = $query->row();
			$copy_data = (array) $row;

			$merge_data = array_merge($copy_data, $data); // 기존 상품 정보와 등록 정보 병합
			//debug($merge_data);exit;

			// 상품번호가 없으면 복사불가
			if(!$row->GoodsNo || $row->GoodsNo == '0')
			{
				echo '{"info":{"success":false,"text":"상품번호가 없는 상품은 복사가 않됩니다."}}';
				exit;
			}

			// 회원 상품이 맞는지 확인
			if($row->user_id != $user_id)
			{
				echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
				exit;
			}

			// 상품 이미지 확인
			$GoodsImageArr = $this->goods_image_check($merge_data);
			//debug($GoodsImageArr);exit;
			//$GoodsImageArr = explode('||', $merge_data['GoodsImageList']);
			$merge_data['GoodsImage'] = $GoodsImageArr[0];
			$SellingPeriodArr = explode("|", $merge_data['SellingPeriod']);
			$merge_data['SellingPeriod'] = $SellingPeriodArr[2];
			$merge_data['SellingPeriodStart'] = $SellingPeriodArr[0];
			$merge_data['SellingPeriodEnd'] = $SellingPeriodArr[1];
			$insert_data = array(
						   'user_id'					=> $user_id,
						   'GdsMstId'					=> $merge_data['GdsMstId'],
						   'market'						=> $merge_data['market'],
						   'Category1'					=> $merge_data['Category1'],
						   'Category2'					=> $merge_data['Category2'],
						   'Category3'					=> $merge_data['Category3'],
						   'Category4'					=> $merge_data['Category4'],
						   'GoodsName'					=> $merge_data['GoodsName'],
						   'CatalogName'				=> $merge_data['CatalogName'],
						   'BrandName'					=> $merge_data['BrandName'],
						   'MakerName'					=> $merge_data['MakerName'],
						   'SellingPeriod'				=> $merge_data['SellingPeriod'],
						   'SellingPeriodStart'			=> $merge_data['SellingPeriodStart'],
						   'SellingPeriodEnd'			=> $merge_data['SellingPeriodEnd'],
						   'GoodsPrice'					=> $merge_data['GoodsPrice'],
						   'GoodsCount'					=> $merge_data['GoodsCount'],
						   'GoodsOptionsUseSetting'		=> $merge_data['GoodsOptionsUseSetting'],
						   'GoodsImage'					=> $merge_data['GoodsImage'],
						   'CommonDeliveryWayOPTSEL'	=> $merge_data['CommonDeliveryWayOPTSEL'],
						   'DeliveryCOMP'				=> $merge_data['DeliveryCOMP'],
						   'ShipmentPlaceNo'			=> $merge_data['ShipmentPlaceNo'],
						   'DeliveryFeeType'			=> $merge_data['DeliveryFeeType'],
						   'NoticeItemGroupNo'			=> $merge_data['NoticeItemGroupNo'],
						   'created'					=> $this->info['created']
			);
			$this->db->insert('goods', $insert_data);
			if($this->db->affected_rows() > 0)
			{
				$goods_id = $this->db->insert_id();
				$merge_data['id'] = $goods_id;
				$merge_data['GoodsId'] = $goods_id;
				$market = $merge_data['market'];

				// 상품별 상세정보 테이블
				$insert_data2 = array(
							   'goods_id'			=> $goods_id,
							   'GoodsOptVal'		=> $merge_data['GoodsOptVal'],
							   'Description'		=> $merge_data['Description'],
							   'NoticeItemCodes'	=> $merge_data['NoticeItemCodes'],
							   'GoodsInsertJson'	=> $post_json,
							);
				$this->db->insert('goods_detail', $insert_data2);

				// 상품별 이미지정보 테이블
				$insert_data3 = array(
							   'goods_id'			=> $goods_id
							);

				if($market == 'A' || $market == 'B')
				{
					foreach($GoodsImageArr AS $k => $v) $insert_data3['img'.$k] = $v;
				}
				if($market == 'C')
				{
					foreach($GoodsImageArr AS $k => $v)
					{
						if($k < 5) $insert_data3['img'.$k] = $v;
					}
				}
				if($market == 'D')
				{
					foreach($GoodsImageArr AS $k => $v)
					{
						if($k < 1) $insert_data3['img'.$k] = $v;
					}
				}
				$this->db->insert('goods_image', $insert_data3);

			} // if($this->db->affected_rows() > 0)

			// 마켓별 소켓 복사
			$this->market_update($goods_id, 'CP', $merge_data);

			//delete_files($this->tmp_upload_url, true);
			//@rmdir($files_url);
			echo '{"info":{"success":true,"text":"'.$data_gb_txt.' 완료입니다."}}';

		} // 복사
	}

    // 상품별 옵션자체코드 자동생성(사이즈 & 색상)
    function size_color_code_make($goods_id, $size, $color)
    {
        $cnt = 0;
        $sizeArr = explode(',', $size);
        $colorArr = explode(',', $color);

        foreach ($sizeArr as $sVal)
        {
            $data = '';
            foreach ($colorArr as $cVal)
            {
                // if(!$sVal || !$cVal) continue;

                $cnt++;
                $code = sprintf('%04d', $cnt);

                $query1 = $this->db->query("SELECT id FROM goods_option_code WHERE goods_id='{$goods_id}' AND code='{$code}'");
                $row1 = $query1->row();
                $goc_id = $row1->id;

                // 상품별 옵션자체코드 테이블
                $data = array(
                    'goods_id'	=> $goods_id,
                    'code'		=> $code,
                    'size'		=> $sVal,
                    'color'	    => $cVal,
                );

                if( $goc_id > 0 ) {
                    $this->db->where('id', $goc_id);
                    $this->db->update('goods_option_code', $data);
                } else {
                    $data['created'] = $this->info['created'];
                    $this->db->insert('goods_option_code', $data);
                }
            }
        }
    }

    // 매입처명 확인 후 계약 총 상품수 / 추가 상품금액 확인(2021.11.03)
	function wholesalebiz_entered_goods($goods_id, $data)
	{
        // $goods_id = 1;
        // $data['GoodsEtc6'] = 'MK&Song'; // test
        // debug($goods_id);
        // debug($data);
		$this->load->library('wholesalebiz_lib');
        $this->wholesalebiz_lib->wholesalebiz_entered_goods($goods_id, $data);
    }

	// 리얼 상품 수정 처리
	function real_update_process($data)
	{
		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		// 상품별 상세정보 테이블 GoodsInsertJson 필드 저장
		$post_json = json_encode($data);
		//debug($post_json);

		if(isset($data['GoodsExcel'])) $ErrorRtn = true;
		else $ErrorRtn = false;
		//debug($ErrorRtn);exit;

		// 수정
		if($data['GoodsCmd'] == '2')
		{
			//debug($data);exit;
			$data_gb_txt = '상품수정';

			// 수정 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.Description, GSD.NoticeItemCodes, GSD.MarketInsertJson,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods_cron			AS GS LEFT OUTER JOIN
							goods_detail_cron	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image_cron	As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$data['GoodsId']}'
			";
			$query = $this->db->query($sql);
			$row = $query->row();
			//debug($row);

			// 상품번호가 없으면 수정불가
			if(!$row->GoodsNo || $row->GoodsNo == '0')
			{
				echo '{"info":{"success":false,"text":"상품번호가 없는 상품은 수정이 불가합니다."}}';
				exit;
			}

			// 회원 상품이 맞는지 확인
			if($row->user_id != $user_id)
			{
				echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
				exit;
			}

			$data['Category1'] = $row->Category1;
			$data['Category2'] = $row->Category2;
			$data['Category3'] = $row->Category3;

			$data['market'] = $row->market;
			$data['GoodsNo'] = $row->GoodsNo;
			$data['MarketInsertJson'] = $row->MarketInsertJson;

			$data['AfterDays'] = ($data['AfterDays'])?$data['AfterDays']:'';
			$data['StyleW'] = ($data['StyleW'])?$data['StyleW']:'';

			// 상품 이미지 확인 및 등록
			if($data['GoodsImageList'])
			{
				$data['GoodsImageCheck'] = array(); // 이미지명 등록 체크
				$GoodsImageArr = explode('||', $data['GoodsImageList']);
				//debug($GoodsImageArr);
				$tmp_file_arr = get_filenames($this->tmp_upload_url); // 임시 이미지 배열
				//debug($tmp_file_arr);

				$goods_image_update_data = array(); // 이미지 정보 테이블 업데이트 필드셋
				foreach($GoodsImageArr AS $k => $v)
				{
					if($v)
					{
						// 이미지명이 임시경로에 있는지 확인(in_array()가 대소문자를 구분), 있으면 신규 이미지등록
						if( in_array($v, $tmp_file_arr) )
						{
							$data['GoodsImageCheck'][$v] = 'Y'; // 신규 이미지 체크

							// 메인이미지만 썸네일 저장
							if($k == 0)
							{
								$config['source_image']	= $this->tmp_upload_url.'/'.$v;
								$config['new_image']	= $this->tmp_upload_url.'/thumbnail/';
								if(!is_dir($config['new_image'])) mkdir($config['new_image'], 0755, TRUE);

								if(!is_file($config['new_image'].$v))
								{
									// thumbnail 생성
									$this->load->library('image_lib');
									$config['image_library'] = 'gd2';
									$config['maintain_ratio'] = TRUE;
									$config['width']	= 100;
									$config['height']	= 100;
									//debug($config);
									$this->image_lib->initialize($config);
									$this->image_lib->resize();
								}
							}
						}
						else
							$data['GoodsImageCheck'][$v] = 'N';

						$goods_image_update_data['img'.$k] = $v;
					}
				}
			}
			//debug($data);

			// 마켓별 소켓 수정
			$rtn = $this->market_update($data['GoodsId'], 'UP', $data);
			//debug($rtn);

			if($rtn == 'Y')
				echo '{"info":{"success":true,"text":"'.$data_gb_txt.' 완료입니다."}}';
			else
				echo '{"info":{"success":false,"text":"'.$data_gb_txt.' 오류입니다."}}';
		}
	}

	// 상품개별삭제
	function erase($goods_id='', $rtnView='')
	{
		$this->load->model('goods_m');
		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$goods_id)
			$goods_id = $this->input->post('goodsId');

		//$goods_img_path = '/home/autoda/data/files/goods/'.$user_id.'/'.$goods_id.'/';

		if($goods_id > 0)
		{
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.Description, GSD.NoticeItemCodes,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods AS		GS LEFT OUTER JOIN
							goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image		As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$goods_id}'
			";
			//debug($sql);
			$query = $this->db->query($sql);
			$row = $query->row();

			// 회원 상품이 맞는지 확인
			if($row->user_id == $user_id)
			{
				// 상품번호가 존재하면 해당 마켓별 상품 삭제 후 디비 삭제
				if($row->GoodsNo != '0')
				{
					//debug($row->market);exit;
					$rtn = $this->market_delete($row->market, trim($row->GoodsNo));

					if($rtn)
					{
						$this->db->delete('goods', array('id' => $goods_id));
						$tables = array('goods_detail', 'goods_image', 'goods_wholesale_contract');
						$this->db->where('goods_id', $goods_id);
						$this->db->delete($tables);
					}
					else
					{
						if($rtnView)
							return '{"info":{"success":false,"text":"처리 오류(2)입니다."}}';
						else
						{
							echo '{"info":{"success":false,"text":"처리 오류(2)입니다."}}';
							exit;
						}
					}
				}
				else
				{
					$this->db->delete('goods', array('id' => $goods_id));
					$tables = array('goods_detail', 'goods_image', 'goods_wholesale_contract');
					$this->db->where('goods_id', $goods_id);
					$this->db->delete($tables);
				}

				// 상품 이미지 삭제
				//delete_files($goods_img_path, true);
				//@rmdir($goods_img_path);

				// 상품 액션(삭제) 로그
				$this->goods_m->action_logs($this->session->userdata('user_id'), $goods_id, 'C');

				if($rtnView)
					return '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
				else
					echo '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
			}
			else
			{
				if($rtnView)
					return '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
				else
				{
					echo '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
					exit;
				}
			}
		}
	}

	// 상품 선택 삭제
	function erase_select()
	{
		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($goodsId);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		$deleting_cnt = count($goodsId); // 삭제할 상품수
		$deletede_cnt = 0; // 삭제된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					$deletede_success = 0; // 마켓별 상품 삭제 성공수

					// 등록 상품 정보 확인
					$sql2 = " SELECT id, market, GoodsNo FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$goods_id}' ";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row2)
						{
							//debug($row2->id);
							$rtn_json = $this->erase($row2->id, '1');
							$rtn_arr = json_decode($rtn_json, 1);
							if($rtn_arr['info']['success']) $deletede_success++; // 삭제 성공 시 카운트
						}
						//exit;

						//debug($rtn_arr);

						if($master_goods_cnt == $deletede_success)
						{
							$this->db->delete('goods_master', array('id' => $goods_id));
							$deletede_cnt++;
						}
					}
					else
					{
						$this->db->delete('goods_master', array('id' => $goods_id));
						$deletede_cnt++;
					}
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$deleting_cnt.' 개] 중 ['.$deletede_cnt.' 개] 상품이 삭제 되었습니다."}}';
	}

	// 등록 상품 등록일 업데이트 = 등록일을 재등록일로 변경(2021.10.20)
	function goods_created_set()
	{
		$this->load->model('goods_m');
		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		$goods_id = $this->input->post('goodsId');

		//$goods_img_path = '/home/autoda/data/files/goods/'.$user_id.'/'.$goods_id.'/';

		if($goods_id > 0)
		{
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.user_id
						FROM
							goods AS		GS LEFT OUTER JOIN
							goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image		As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$goods_id}'
			";
			//debug($sql);
			$query = $this->db->query($sql);
			$row = $query->row();

			// 회원 상품이 맞는지 확인
			if($row->user_id == $user_id)
			{
                $goods_data['re_created'] = $this->info['created'];

                $this->db->where('id', $goods_id);
                $this->db->update('goods', $goods_data);

				// 상품 액션(F:재등록일 수정) 로그
				$this->goods_m->action_logs($this->session->userdata('user_id'), $goods_id, 'G');

				echo '{"info":{"success":true,"text":"상품 재등록일이 수정되었습니다."}}';
			}
			else
				echo '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
		}
	}

	// 상품 선택 담기
	function plus_minus_select()
	{
		$user_id = $this->session->userdata('user_id');
		$gb = $this->input->post('gb');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$gb || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		if($gb == 'P') $SendCheck = 'Y';
		if($gb == 'M') $SendCheck = 'N';

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = " SELECT id, market, GoodsNo FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$goods_id}' ";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row2)
						{
							//debug($row2->id);
							$update_data = array(
								'SendCheck' => $SendCheck
							);
							$this->db->where('id', $row2->id);
							$this->db->update('goods', $update_data);

							$success_cnt++; // 성공 시 카운트
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 상품 선택 상태 변경
	function goodsetc52_select()
	{
		$user_id = $this->session->userdata('user_id');
		$goodsetc52 = $this->input->post('goodsetc52');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$goodsetc52 || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = " SELECT id, market, GoodsNo, activated FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$goods_id}' ";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row2)
						{
							//debug($row2->id);
							$update_data = array(
								'GoodsEtc52' => $goodsetc52
							);

							$this->db->where('id', $row2->id);
							$this->db->update('goods', $update_data);

							$success_cnt++; // 성공 시 카운트
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 단독상품 선택 변경
	function goodsonly_select()
	{
		$user_id = $this->session->userdata('user_id');
		$goodsonly = $this->input->post('goodsonly');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$goodsonly || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = " SELECT id, market, GoodsNo, GoodsOnly, activated FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$goods_id}' ";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row2)
						{
							// 기존 옵션값과 변경 옵션값이 다를 때 반영
							if($row2->GoodsOnly != $goodsonly)
							{
								//debug($row2->id);
								$update_data = array(
									'GoodsOnly' => $goodsonly,
									'GoodsOnlyDay' => $this->info['today']
								);

								$this->db->where('id', $row2->id);
								$this->db->update('goods', $update_data);

								// 단독상품옵션 변경 로그 데이타
								$insert_data3 = array(
									'before'	=> $row2->GoodsOnly,
									'after'		=> $goodsonly,
									'user_id'	=> $user_id,
									'goods_id'	=> $row2->id,
									'user_ip'	=> $this->input->ip_address()
								);
								$this->db->insert('goods_only_logs', $insert_data3);

								$success_cnt++; // 성공 시 카운트
							}
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}
	
	function goodsbiz_select_test() {
		$this->load->model('goods_m');
		$goodsId = array('75450', '71685');
		$alim_users = $this->goods_m->get_user_goods_alimtalk($goodsId);
		var_dump($alim_users);
		echo "<br />";
		for ($i=0; $i<count($alim_users); $i++) {
			array_push($alim_users[$i], array('goods'=>array()));
		}
		for ($i=0; $i<count($alim_users); $i++) {
			array_push($alim_users[$i][0]['goods'], array('good_name'=>'aaa', 'good_cnt'=>3));
			array_push($alim_users[$i][0]['goods'], array('good_name'=>'bbb', 'good_cnt'=>2));
		}
		var_dump($alim_users);
	}

	// 선택상품 업무진행상태 변경
	function goodsbiz_select()
	{
		$this->load->model('goods_m');

		$user_id = $this->session->userdata('user_id');
		$goodsbiz = $this->input->post('goodsbiz');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$goodsbiz || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수
		
		//알림톡발송대상
		$alim_users = $this->goods_m->get_user_goods_alimtalk($goodsId);
		for ($i=0; $i<count($alim_users); $i++) {
			array_push($alim_users[$i], array('goods'=>array()));
		}

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = " SELECT id, market, GoodsNo, BizProgress, activated, GoodsEtc6, GoodsEtc5, GoodsEtc9, OptionColor, OptionSize FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$goods_id}' ";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row2)
						{
							// 기존 옵션값과 변경 옵션값이 다를 때 반영
							if($row2->BizProgress != $goodsbiz)
							{
								//debug($row2->id);
								$update_data = array(
									'BizProgress' => $goodsbiz,
									'BizProgressUpdate' => $this->info['created']
								);

								$this->db->where('id', $row2->id);
								$this->db->update('goods', $update_data);

								// 상품 업무진행상태 로그
								$this->goods_m->biz_logs($this->session->userdata('user_id'), $row2->id, $goodsbiz, $this->info['created']);
								
								//알림톡 상품내역
								for ($i=0; $i<count($alim_users); $i++) {
									if ($alim_users[$i]['username'] == $row2->GoodsEtc6) {
										array_push($alim_users[$i][0]['goods'], array('id'=>$row2->id, 'name'=>$row2->GoodsEtc5, 'price'=>$row2->GoodsEtc9, 'color'=>$row2->OptionColor, 'size'=>$row2->OptionSize));
									}
								}

								$success_cnt++; // 성공 시 카운트
							}
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}
		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
		//var_dump($alim_users);
		
		//알림톡
		if ($goodsbiz == 'B2' || $goodsbiz == 'H1' || $goodsbiz == 'J2') {
			if ($goodsbiz == 'B2') {
				$templtCode = 'TQ_2458'; //진행컨펌완료
				$emtitle = '뉴톡[Newtalk]';
			}
			else if ($goodsbiz == 'H1') {
				$templtCode = 'TQ_2457'; //이미지컨펌완료(2023.12.29 사방넷등록대기로 변경 G2->H1)
				$emtitle = '뉴톡';
			}
			else if ($goodsbiz == 'J2') {
				$templtCode = 'TQ_2459'; //샘플반납완료
				$emtitle = '뉴톡[Newtalk]';
			}
			$template = $this->aligo_sms->get_template_arr($templtCode);
			//var_dump($template);
			$cnt_id = 0;
			$variables_def = array('tpl_code' => $templtCode);
			$variables = $variables_def;
			
			for ($i=0; $i<count($alim_users); $i++) {
				$content = $this->aligo_sms->create_content2($templtCode, $template, $alim_users[$i], '');
				//var_dump($content);
				if ($alim_users[$i]['shop_staff_hp'] != '') {
					$cnt_id++;
					$variables = array_merge($variables, array(
						'emtitle_'.$cnt_id	=> $emtitle,
						'receiver_'.$cnt_id	=> $alim_users[$i]['shop_staff_hp'],
						'recvname_'.$cnt_id	=> $alim_users[$i]['username'],
						'subject_'.$cnt_id	=> $content['subject'],
						'message_'.$cnt_id	=> $content['message'],
						'fsubject_'.$cnt_id	=> $content['subject'],
						'fmessage_'.$cnt_id	=> $content['message']
					));
					if (isset($content['button'])) {
						$variables = array_merge($variables, array('button_'.$cnt_id=>$content['button']));
					}
					if ($cnt_id == 500) {
						$result = $this->aligo_sms->send_sms3($variables);
						$variables = $variables_def; $cnt_id = 0;
					}
				}
				if ($alim_users[$i]['shop_tel'] != '' && strlen($alim_users[$i]['shop_tel']) > 10 && $alim_users[$i]['shop_tel'] != $alim_users[$i]['shop_staff_hp']) {
					$cnt_id++;
					$variables = array_merge($variables, array(
						'emtitle_'.$cnt_id	=> $emtitle,
						'receiver_'.$cnt_id	=> $alim_users[$i]['shop_tel'],
						'recvname_'.$cnt_id	=> $alim_users[$i]['username'],
						'subject_'.$cnt_id	=> $content['subject'],
						'message_'.$cnt_id	=> $content['message'],
						'fsubject_'.$cnt_id	=> $content['subject'],
						'fmessage_'.$cnt_id	=> $content['message']
					));
					if (isset($content['button'])) {
						$variables = array_merge($variables, array('button_'.$cnt_id=>$content['button']));
					}
					if ($cnt_id == 500) {
						$result = $this->aligo_sms->send_sms3($variables);
						$variables = $variables_def; $cnt_id = 0;
					}
				}
				if ($alim_users[$i]['delegate_hp'] != '' && strlen($alim_users[$i]['delegate_hp']) > 10 && $alim_users[$i]['delegate_hp'] != $alim_users[$i]['shop_tel'] && $alim_users[$i]['delegate_hp'] != $alim_users[$i]['shop_staff_hp']) {
					$cnt_id++;
					$variables = array_merge($variables, array(
						'emtitle_'.$cnt_id	=> $emtitle,
						'receiver_'.$cnt_id	=> $alim_users[$i]['delegate_hp'],
						'recvname_'.$cnt_id	=> $alim_users[$i]['username'],
						'subject_'.$cnt_id	=> $content['subject'],
						'message_'.$cnt_id	=> $content['message'],
						'fsubject_'.$cnt_id	=> $content['subject'],
						'fmessage_'.$cnt_id	=> $content['message']
					));
					if (isset($content['button'])) {
						$variables = array_merge($variables, array('button_'.$cnt_id=>$content['button']));
					}
					if ($cnt_id == 500) {
						$result = $this->aligo_sms->send_sms3($variables);
						$variables = $variables_def; $cnt_id = 0;
					}
				}
			}
			//var_dump($variables);
			if ($cnt_id > 0) $result = $this->aligo_sms->send_sms3($variables);
			//var_dump($result);
		}
	}

	// 상품 선택 노출/미노출
	function activated_select()
	{
		$user_id = $this->session->userdata('user_id');
		$gb = $this->input->post('gb');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$gb || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		if($gb == 'Y') $activated = 'Y';
		if($gb == 'N') $activated = 'N';

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = " SELECT id, market, GoodsNo, activated FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$goods_id}' ";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row2)
						{
							//debug($row2->id);
							$update_data = array(
								'activated' => $activated
							);

							// 노출이 'N' 에서 'Y' 로 변경시만 노출일 수정(2020-04-03)
							if($row2->activated == 'N' && $activated == 'Y') $update_data['activated_day'] = date('Y-m-d', time());
							// 노출이 'N' 으로 변경시는 노출일 초기화(2020-04-03)
							// if($activated == 'N') $update_data['activated_day'] = '0000-00-00';
							// 노출이 'N' 으로 변경시는 노출일 초기화(2021-02-03)
							if($activated == 'N') $update_data['activated_day'] = date('Y-m-d', time());

							$this->db->where('id', $row2->id);
							$this->db->update('goods', $update_data);

							$success_cnt++; // 성공 시 카운트
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 상품 선택 미니몰 노출/미노출
	function mall_activated_select()
	{
		$user_id = $this->session->userdata('user_id');
		$gb = $this->input->post('gb');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$gb || !$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		if($gb == 'Y') $activated = 'Y';
		if($gb == 'N') $activated = 'N';

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = " SELECT id, market, GoodsNo FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$goods_id}' ";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row2)
						{
							//debug($row2->id);
							$update_data = array(
								'mall_activated' => $activated
							);
							$this->db->where('id', $row2->id);
							$this->db->update('goods', $update_data);

							$success_cnt++; // 성공 시 카운트
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 상품 선택 이미지 압축 다운
	function zip_select()
	{
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.Description, GSD.NoticeItemCodes,
								GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						foreach($query2->result() as $row)
						{
							$img_home = $this->config->config['user_temp_image_dir'].$row->user_id;		// 절대경로
							$img_path = '/data/files/goods/'.$row->user_id;								// 페이지경로
							//debug($img_home);

						for($j=0; $j<15; $j++)
						{
							$img_name = $row->{'img'.$j};
							if($img_name)
							{
								// ===== 2026-01-29 v4: zip_select thumbnail 필터링 START =====
								// thumbnail 파일명 패턴 차단
								if (preg_match("/thumbnail/i", $img_name)) {
									
									continue;
								}
								// ===== 2026-01-29 v4: zip_select thumbnail 필터링 END =====
								
								$zip_path = $img_home.'/'.$img_name;
								//debug($zip_path);
								if(is_file($img_home.'/'.$img_name))
								{
									$this->zip->read_file($zip_path);
									//debug($zip_path);
								}
							}
						}
							//$this->zip->download('myarchive.zip');

							$update_data = array(
								'SendCheck' => $SendCheck
							);
							//$this->db->where('id', $row2->id);
							//$this->db->update('goods', $update_data);

							$success_cnt++; // 성공 시 카운트
						}
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다.","url":"/data/files/goods/myarchive.zip"}}';
	}


	// 판매처 FTP 상품 선택 전송
	function goods_ftp_send_select()
	{
		$post_val = $this->input->post();
		// debug($post_val);exit;

		if(!$post_val['GoodsId'] || count($post_val['ftpid']) < 1)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

        $ftpids = $post_val['ftpid'];
        $goodsIdArr = explode(',', $post_val['GoodsId']);
		// debug($goodsIdArr);exit;

		$setting_cnt = count($goodsIdArr); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsIdArr AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
                $GoodsCode = '';

                // 등록 상품 정보 확인
                $sql = "SELECT GoodsCode FROM goods WHERE id='{$goods_id}'";
                $query = $this->db->query($sql);
			    $row = $query->row();
                $GoodsCode = $row->GoodsCode;
		        // debug($GoodsCode);exit;

                if($GoodsCode)
                {
                    if($this->goods_ftp_send($ftpids, $GoodsCode))
						$success_cnt++; // 성공 시 카운트
                }
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 판매처 FTP 상품 전송
	function goods_ftp_send($ftpids, $GoodsCode)
	{
        $this->load->library('ftp');

        if(count($ftpids) < 1) return false;
        if(!$GoodsCode) return false;

		foreach($ftpids AS $k => $id)
        {
            // FTP 관리 목록
            $query = $this->db->query("SELECT * FROM store_ftp_config WHERE id={$id}");
            $row = $query->row();

            if(!$row->id) return false;

            $config['hostname'] = $row->ftp_host;
            $config['username'] = $row->ftp_id;
            $config['password'] = $row->ftp_pw;
            // $config['debug'] = TRUE;

            if(!$this->ftp->connect($config)) return false;

            $mkdir_path = '';
            if($row->ftp_folder)
                $mkdir_path = '/'.$row->ftp_folder.'/'.$GoodsCode.'/';
            else
                $mkdir_path = '/'.$GoodsCode.'/';
		    // debug($this->ftp->list_files('/imgtest/'));

            // 이미지 루트 폴더 확인 없으면 생성
            if(!$this->ftp->list_files('/'.$row->ftp_folder.'/'))
            {
		        // debug($mkdir_path);
                if(!$this->ftp->mkdir('/'.$row->ftp_folder.'/', DIR_WRITE_MODE))
                    $this->goods_ftp_close_return($this->ftp, true);
            }

            // 이미지 폴더가 있으면 삭제 후 생성
            if($this->ftp->list_files($mkdir_path) !== false)
            {
                if($this->ftp->delete_dir($mkdir_path))
                {
		            // debug($mkdir_path);
                    if($this->ftp->mkdir($mkdir_path, DIR_WRITE_MODE))
                    {
                        $copyPathDir = '/home/danharoo/www/data/files/goods/goodscode/img/'.$GoodsCode.'/';

                        if($this->ftp->mirror($copyPathDir, $mkdir_path))
                            return $this->goods_ftp_close_return($this->ftp, true);
                        else
                            return $this->goods_ftp_close_return($this->ftp, false);
                    }
                    else
                        return $this->goods_ftp_close_return($this->ftp, false);
                }
            }
            else
            {
		        // debug($this->ftp->list_files($mkdir_path));
                if($this->ftp->mkdir($mkdir_path, DIR_WRITE_MODE))
                {
                    $copyPathDir = '/home/danharoo/www/data/files/goods/goodscode/img/'.$GoodsCode.'/';

                    if($this->ftp->mirror($copyPathDir, $mkdir_path))
                        return $this->goods_ftp_close_return($this->ftp, true);
                    else
                        return $this->goods_ftp_close_return($this->ftp, false);
                }
                else
                    return $this->goods_ftp_close_return($this->ftp, false);

            }
        }
	}

	function goods_ftp_close_return($ftp, $boolean)
    {
        $ftp->close();
        return $boolean;
    }

	// 마켓별 카테고리
	function category($scrap, $market, $code=NULL, $json=FALSE)
	{
		//debug($scrap, 1);
		$json_cate1 = ARRAY();

		if($scrap)
		{
			$market_name = $market.'_name';
			$market_cate1 = $market.'_cate1';

			$this->config_vars['path_cookie'][$market] = $scrap->config_vars['path_cookie'][$market];
			$this->view_data[$market_name] = $scrap->config_vars['open_market2'][$market];

			// 11번가
			if($market == "C")
			{
				$this->view_data['C_cate1'] = $scrap->get_site_cate('C', $code);
				//debug($this->view_data['C_cate1']);
				$this->view_data['C_cate1'] = substr_replace( $this->view_data['C_cate1'], "", 0, 1 );
				$this->view_data['C_cate1'] = substr_replace( $this->view_data['C_cate1'], "", -1 );

				if(!$code)
				{
					$this->view_data['C_cate1'] = str_replace("'conditionList':'[]'", '"conditionList":[]', $this->view_data['C_cate1'] );
					$this->view_data['C_cate1'] = str_replace("'lCategoryList':'", '"lCategoryList":', $this->view_data['C_cate1'] );
					$this->view_data['C_cate1'] = str_replace("','mCategoryList':'[]'", ',"mCategoryList":[]', $this->view_data['C_cate1'] );
					$this->view_data['C_cate1'] = str_replace("'sCategoryList':'[]'", '"sCategoryList":[]', $this->view_data['C_cate1'] );
					$C_cate1 = json_decode($this->view_data['C_cate1'], 1);
					$this->view_data['C_cate1'] = $C_cate1['lCategoryList'];
				}
				else
				{
					$C_cate1 = json_decode($this->view_data['C_cate1'], 1);
					//debug($C_cate1);
					$this->view_data['C_cate1'] = $C_cate1;
				}

				if($json)
					$this->view_data['C_cate1'] = json_encode($this->view_data['C_cate1']);
			}
			// 신상마켓
			else if($market == "G")
			{
				$this->view_data['G_cate1'] = $scrap->get_site_cate('G', $code);
				//debug($this->view_data['G_cate1']);
			}
			else
			{
				$json_cate1 = json_decode($scrap->get_site_cate($market, $code), 1);

				// ESMPLUS
				if(isset($json_cate1['Result']))
					$this->view_data[$market_cate1] = $json_cate1['Result'];
				// 스토어팜
				else if(isset($json_cate1['htReturnValue']))
					$this->view_data[$market_cate1] = $json_cate1['htReturnValue'];
				// 위메프
				else if(isset($json_cate1['category_data']))
					$this->view_data[$market_cate1] = $json_cate1['category_data'];
				// 쿠팡
				else
					$this->view_data[$market_cate1] = $json_cate1;

				if($json)
					$this->view_data[$market_cate1] = json_encode($this->view_data[$market_cate1]);
			}
		}
	}

	// 마켓별 카테고리(중분류 부터 사용) GET 방식 호출 함수
	function getcategory()
	{
		if(@$this->uri->segment(3) && @$this->uri->segment(4))
		{
			$market = $this->uri->segment(3);
			$code = $this->uri->segment(4);
			if(@$this->uri->segment(5)) $code .= '|'.$this->uri->segment(5);
			//debug($market);debug($code);

			$market_cate1 = $market.'_cate1';

			switch($market)
			{
				// 옥션
				case "A":
					$this->esmplus_scrap->login('A');
					if($this->esmplus_scrap->config_vars['path_cookie']['A']['login']) $this->category($this->esmplus_scrap, 'A', $code, TRUE);
					break;
				// 지마켓
				case "B":
					$this->esmplus_scrap->login('B');
					if($this->esmplus_scrap->config_vars['path_cookie']['B']['login']) $this->category($this->esmplus_scrap, 'B', $code, TRUE);
					break;
				// 11번가
				case "C":
					$this->elevenst_scrap->login('C');
					//debug($this->elevenst_scrap->config_vars);
					if($this->elevenst_scrap->config_vars['path_cookie']['C']['login']) $this->category($this->elevenst_scrap, 'C', $code, TRUE);
					break;
				// 쿠팡
				case "D":
					$this->coupang_scrap->login('D');
					//debug($this->coupang_scrap->config_vars);
					if($this->coupang_scrap->config_vars['path_cookie']['D']['login']) $this->category($this->coupang_scrap, 'D', $code, TRUE);
					break;
				// 스토어팜
				case "E":
					$this->storefarm_scrap->login('E');
					//debug($this->coupang_scrap->config_vars);
					if($this->storefarm_scrap->config_vars['path_cookie']['E']['login']) $this->category($this->storefarm_scrap, 'E', $code, TRUE);
					break;
				// 위메프
				case "F":
					$this->wemakeprice_scrap->login('F');
					//debug($this->coupang_scrap->config_vars);
					if($this->wemakeprice_scrap->config_vars['path_cookie']['F']['login']) $this->category($this->wemakeprice_scrap, 'F', $code, TRUE);
					break;
				// 신성마켓
				case "G":
					$this->sinsang_scrap->login('G');
					//debug($this->sinsang_scrap->config_vars);
					if($this->sinsang_scrap->config_vars['path_cookie']['G']['login']) $this->category($this->sinsang_scrap, 'G', $code, TRUE);
					break;
			}

			echo $this->view_data[$market_cate1];

		}
		else
			echo 'false';
	}

	// 상품의 상품군 GET 방식 호출 함수
	function noticeitemcodes()
	{
		if(@$this->uri->segment(3))
		{
			$notice_item_group_no = $this->uri->segment(3);

			// 기본값 마켓은 ESMPLUS 상품군
			$this->esmplus_scrap->login('A');
			if($this->esmplus_scrap->config_vars['path_cookie']['A']['login'])
			{
				echo $this->esmplus_scrap->notice_item_codes('A', $notice_item_group_no);
			}
			else
				echo 'false';
		}
		else
			echo 'false';
	}

	// 상품등록
	function market_update($GoodsId, $GB='', $Data='', $ALL=FALSE)
	{
		$rst_rtn = 'N';
		$user_id = $this->session->userdata('user_id');

		// 신규등록
		if(!$GB)
		{
			if($GoodsId > 0)
			{
				// 등록 상품 정보 확인
				$sql = "SELECT
							GS.*,
							GSD.GoodsOptVal, GSD.Description, GSD.NoticeItemCodes,
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
							FROM
								goods AS		GS LEFT OUTER JOIN
								goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
								goods_image		As GSI ON GS.id = GSI.goods_id
							WHERE GS.id='{$GoodsId}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();

				// 회원 상품이 맞는지 확인
				if($row->user_id == $user_id)
				{
					$rtn = '';

					$start = 'market_'.$row->market.'_start';
					$end = 'market_'.$row->market.'_end';
					$this->benchmark->mark($start); // 마켓별 벤치마크 시작
					switch($row->market)
					{
						// 옥션
						case "A":
						// 지마켓
						case "B":
							//debug($row);
							if($ALL == TRUE)
								$rtn = $this->esmplus_scrap->goods_insert($row, TRUE);
							else
								$rtn = $this->esmplus_scrap->goods_insert($row);
							break;
						// 11번가
						case "C":
							$rtn = $this->elevenst_scrap->goods_insert($row);
							break;
						// 쿠팡
						case "D":
							$rtn = $this->coupang_scrap->goods_insert($row);
							break;
						// 스토어팜
						case "E":
							$rtn = $this->storefarm_scrap->goods_insert($row);
							break;
						// 위메프
						case "F":
							$rtn = $this->wemakeprice_scrap->goods_insert($row);
							break;
						// 신상마켓
						case "G":
							$rtn = $this->sinsang_scrap->goods_insert($row);
							break;
					}
					$this->benchmark->mark($end); // 마켓별 벤치마크 시작
					//debug($this->benchmark->elapsed_time($start, $end));
					log_message('debug', $start);
					log_message('debug', print_r($this->benchmark->elapsed_time($start, $end), TRUE));
					log_message('debug', $end);

					if($rtn == 'Y')
						$rst_rtn = 'N';
				}
			}
		}

		// 수정
		if($GB == 'UP')
		{
			//debug($Data);
			switch($Data['market'])
			{
				// 옥션
				case "A":
				// 지마켓
				case "B":
					$rtn = $this->esmplus_scrap->goods_update($Data);
					break;
				// 11번가
				case "C":
					$rtn = $this->elevenst_scrap->goods_update($Data);
					break;
				// 쿠팡
				case "D":
					$rtn = $this->coupang_scrap->goods_update($Data);
					break;
				// 스토어팜
				case "E":
					$rtn = $this->storefarm_scrap->goods_update($Data);
					break;
				// 위메프
				case "F":
					$rtn = $this->wemakeprice_scrap->goods_update($Data);
					break;
				// 신상마켓
				case "G":
					$rtn = $this->sinsang_scrap->goods_update($Data);
					return $rtn;
					break;
			}
		}

		// 복사
		if($GB == 'CP')
		{
			//debug($Data);exit;
			switch($Data['market'])
			{
				// 옥션
				case "A":
				// 지마켓
				case "B":
					$rtn = $this->esmplus_scrap->goods_copy($Data);
					break;
				// 11번가
				case "C":
					$rtn = $this->elevenst_scrap->goods_copy($Data);
					break;
				// 쿠팡
				case "D":
					$rtn = $this->coupang_scrap->goods_copy($Data);
					break;
				// 스토어팜
				case "E":
					$rtn = $this->storefarm_scrap->goods_copy($Data);
					break;
				// 위메프
				case "F":
					$rtn = $this->wemakeprice_scrap->goods_copy($Data);
					break;
			}
		}

	}

	// 상품삭제
	function market_delete($market, $GoodsNo)
	{
		$rtn = false;
		$user_id = $this->session->userdata('user_id');

		//debug($GoodsNo);
		if(!$market || !$GoodsNo) return $rtn;

		switch($market)
		{
			// 옥션
			case "A":
			// 지마켓
			case "B":
				$rtn = $this->esmplus_scrap->goods_delete($market, $GoodsNo);
				break;
			// 11번가
			case "C":
				$rtn = $this->elevenst_scrap->goods_delete($market, $GoodsNo);
				break;
			// 쿠팡
			case "D":
				$rtn = $this->coupang_scrap->goods_delete($market, $GoodsNo);
				break;
			// 스토어팜
			case "E":
				$rtn = $this->storefarm_scrap->goods_delete($market, $GoodsNo);
				break;
			// 위메프
			case "F":
				$rtn = $this->wemakeprice_scrap->goods_delete($market, $GoodsNo);
				break;
			// 신상마켓
			case "G":
				$rtn = $this->sinsang_scrap->goods_delete($market, $GoodsNo);
				break;
		}

		return $rtn;
	}

	// 마스터별 마켓 상품등록
	function master_market_select_update()
	{
		$user_id = $this->session->userdata('user_id');
		$GdsMstId = $this->input->post('mtid');

		if($GdsMstId > 0)
		{
			//debug($GdsMstId);

			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.GoodsOptVal, GSD.Description, GSD.NoticeItemCodes,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods_master	AS GSM LEFT OUTER JOIN
							goods			As GS ON GSM.id = GS.GdsMstId LEFT OUTER JOIN
							goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image		As GSI ON GS.id = GSI.goods_id
						WHERE
							GSM.id='{$GdsMstId}'
			";
			$query = $this->db->query($sql);

			$rtn = '';
			foreach($query->result() AS $row)
			{
				// 회원 상품이고 마켓상품번호가 없는거만  처리
				if( ($row->user_id == $user_id) && ($row->GoodsNo == 0 || !$row->GoodsNo) )
				{
					switch($row->market)
					{
						// 옥션
						case "A":
						// 지마켓
						case "B":
							$rtn = $this->esmplus_scrap->goods_insert($row);
							break;
						// 11번가
						case "C":
							$rtn = $this->elevenst_scrap->goods_insert($row);
							break;
						// 쿠팡
						case "D":
							$rtn = $this->coupang_scrap->goods_insert($row);
							break;
						// 스토어팜
						case "E":
							$rtn = $this->storefarm_scrap->goods_insert($row);
							break;
						// 위메프
						case "F":
							$rtn = $this->wemakeprice_scrap->goods_insert($row);
							break;
						// 신상마켓
						case "G":
							$rtn = $this->sinsang_scrap->goods_insert($row);
							break;
					}
				}
			}

			if($rtn == 'Y')
			{
				echo '{"info":{"success":true,"text":"상품등록 완료"}}';
				return;
			}
			else if($rtn == 'N')
			{
				echo '{"info":{"success":false,"text":"상품등록 실패(2)"}}';
				return;
			}
			else
			{
				echo '{"info":{"success":false,"text":"'.$rtn.'"}}';
				return;
			}
		}

		echo '{"info":{"success":false,"text":"상품등록 실패(1)"}}';
		return;
	}

	// 마켓별 상품등록
	function market_select_update()
	{
		if(@$this->uri->segment(3))
		{
			$user_id = $this->session->userdata('user_id');
			$GoodsId = $this->uri->segment(3);

			if(isset($GoodsId))
			{
				// 등록 상품 정보 확인
				$sql = "SELECT
							GS.*,
							GSD.GoodsOptVal, GSD.Description, GSD.NoticeItemCodes,
							GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
							FROM
								goods AS		GS LEFT OUTER JOIN
								goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
								goods_image		As GSI ON GS.id = GSI.goods_id
							WHERE GS.id='{$GoodsId}'
				";
				$query = $this->db->query($sql);
				$row = $query->row();

				// 회원 상품이 맞는지 확인
				if($row->user_id == $user_id)
				{
					$rtn = '';
					switch($row->market)
					{
						// 옥션
						case "A":
						// 지마켓
						case "B":
							$rtn = $this->esmplus_scrap->goods_insert($row);
							break;
						// 11번가
						case "C":
							$rtn = $this->elevenst_scrap->goods_insert($row);
							break;
						// 쿠팡
						case "D":
							$rtn = $this->coupang_scrap->goods_insert($row);
							break;
						// 스토어팜
						case "E":
							$rtn = $this->storefarm_scrap->goods_insert($row);
							break;
						// 위메프
						case "F":
							$rtn = $this->wemakeprice_scrap->goods_insert($row);
							break;
					}

					if($rtn == 'Y')
						echo '{"info":{"success":true,"text":"상품등록 완료"}}';
					else if($rtn == 'N')
						echo '{"info":{"success":false,"text":"상품등록 실패"}}';
					else
						echo '{"info":{"success":false,"text":"정상적인 데이타가 아닙니다."}}';
				}
				else
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
			}
			else
				echo '{"info":{"success":false,"text":"정상적인 접근(1)이 아닙니다."}}';
		}
		else
			echo '{"info":{"success":false,"text":"정상적인 접근(2)이 아닙니다."}}';
	}

	// 코디상품코드 체크
	function goods_code_check()
	{
		$goodsCode = $this->input->post('goodsCode');

		if(!$goodsCode) {
			echo '{"info":{"success":false, "text":"체크할 상품코드 입력값이 없습니다!"}}';
			exit;
		}

		// 등록 상품 정보 확인
		// $sql = "SELECT count(id) as cnt FROM goods WHERE LOWER(GoodsCode) LIKE '%".strtolower($goodsCode)."%'";
		$sql = "SELECT count(id) as cnt FROM goods WHERE LOWER(GoodsCode) = '".strtolower($goodsCode)."'";
		$query = $this->db->query($sql);
		$row = $query->row();
		if($row->cnt < 1)
		{
			echo '{"info":{"success":false, "text":"체크할 상품코드는 없는 상품입니다!"}}';
			exit;
		}

		echo '{"info":{"success":true, "text":""}}';
		exit;
	}

	// 상품별 코디상품코드 상품 추가 / 삭제 처리 함수
    function goods_code_check_process()
    {
		$gb = $this->input->post('gb');
		$goodsCode = $this->input->post('goodsCode');
		$coordinateCode = $this->input->post('coordinateCode');

		if(!$gb || !$goodsCode || !$coordinateCode) {
			echo '{"info":{"success":false, "text":"정상적인 데이타가 없습니다!"}}';
			exit;
		}

		// 코디상품코드 등록 상품 정보 확인
		// $sql = "SELECT count(id) as cnt FROM goods WHERE LOWER(GoodsCode) LIKE '%".strtolower($goodsCode)."%'";
		$sql = "SELECT count(id) as cnt FROM goods WHERE LOWER(GoodsCode) = '".strtolower($coordinateCode)."'";
		$query = $this->db->query($sql);
		$row = $query->row();
		if($row->cnt < 1)
		{
			echo '{"info":{"success":false, "text":"체크할 코디상품코드는 없는 상품입니다!"}}';
			exit;
		}

        // 추가되는 코디상품에 해당 상품코드가 적용되어 있는지 확인
        $sql = "SELECT
                    GS.id, GSD.CoordiGoodsCodes
                    FROM
                        goods AS		GS LEFT OUTER JOIN
                        goods_detail	As GSD ON GS.id = GSD.goods_id
                    WHERE
                        GS.GoodsCode='{$coordinateCode}'
        ";
        // debug($sql);
		$query = $this->db->query($sql);
		$row = $query->row();
        $CoordiGoodsId = $row->id;
        $CoordiGoodsCodes = $row->CoordiGoodsCodes;

        // 추가
        if($gb == 'A')
        {
            // 코디상품값이 있으면
            if($CoordiGoodsCodes)
            {
                $CoordiGoodsCodesArr = explode(',', $CoordiGoodsCodes);

                // 해당 코디상품에 해당 상품코드가 있는지 확인
                foreach($CoordiGoodsCodesArr AS $code)
                {
                    // 이미 있는 코디상품이면 리턴
                    if(strtolower($code) == strtolower($goodsCode)) {
                        echo '{"info":{"success":false, "text":"해당 상품에 이미 등록되어 있는 코디상품입니다!"}}';
                        exit;
                    }
                }

                // 없는 상품코드이면 추가
                $CoordiGoodsCodes .= ','.$goodsCode;
            }
            else {
                // 없는 상품코드이면 추가
                $CoordiGoodsCodes .= $goodsCode;
            }

            $update_data = array(
                            'CoordiGoodsCodes'	=> $CoordiGoodsCodes
                        );
            $this->db->where('goods_id', $CoordiGoodsId);
            $this->db->update('goods_detail', $update_data);

            echo '{"info":{"success":true, "text":""}}';
            exit;
        }
        // 삭제
        else if($gb == 'D')
        {
            $CoordiGoodsCodesArr = explode(',', $CoordiGoodsCodes);
            $key = array_search(strtolower($code), $CoordiGoodsCodesArr);
            array_splice($CoordiGoodsCodesArr, $key, 1);

            $CoordiGoodsCodes = implode(',', $CoordiGoodsCodesArr);

            $update_data = array(
                            'CoordiGoodsCodes'	=> $CoordiGoodsCodes
                        );
            $this->db->where('goods_id', $CoordiGoodsId);
            $this->db->update('goods_detail', $update_data);

            echo '{"info":{"success":true, "text":""}}';
            exit;
        }
		else {
			echo '{"info":{"success":false, "text":"정상적인 구분값이 아닙니다!"}}';
			exit;
		}
    }

	// 상품코드 목록
	function goods_code()
	{
		//$this->load->helper('directory');
		$this->load->helper('html');

		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');

		//$goods_img_dir = $this->config->item('user_temp_image_dir').'img/';
		//$map = directory_map($goods_img_dir);
		//debug($map);
		//$this->view_data['my_list'] = $map;
		//debug($this->view_data);

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_code.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품코드 목록
	function goods_code_test()
	{
		//$this->load->helper('directory');
		$this->load->helper('html');

		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');

		//$goods_img_dir = $this->config->item('user_temp_image_dir').'img/';
		//$map = directory_map($goods_img_dir);
		//debug($map);
		//$this->view_data['my_list'] = $map;
		//debug($this->view_data);

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_code_test.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품 선택 텍스트파일 만들기
	function goods_select_text_make()
	{
		//$this->load->model('goods_m');
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE (1) AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();

						$data_txt = '';
						if($goods_info->GoodsName)
						{
							$data_txt .= '[ '.$goods_info->GoodsName.' ]'.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ''.$goods_info->Description.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= 'Product Info_'.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - FABRIC(소재) : '.$goods_info->GoodsEtc16.PHP_EOL.PHP_EOL;
							$data_txt .= ' - COLOR(색상) : '.$goods_info->OptionColor.PHP_EOL.PHP_EOL;
							$data_txt .= ' - 원단느낌 : '.$goods_info->GoodsEtc15.PHP_EOL.PHP_EOL;
							$data_txt .= ' - WASHING(세탁방법) : '.$goods_info->GoodsEtc18.PHP_EOL.PHP_EOL;
							$data_txt .= ' - Weight(무게:g) : '.$goods_info->GoodsEtc17.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SIZE(사이즈) : '.$goods_info->OptionSize.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SizeSpec(상세사이즈) : '.$goods_info->GoodsEtc14.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조사 : '.$goods_info->MakerName.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조일 : '.$goods_info->GoodsEtc20.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조국 : '.$goods_info->GoodsEtc21.PHP_EOL.PHP_EOL;
							$data_txt .= '- 품질보증기준 : '.$goods_info->GoodsEtc22.PHP_EOL.PHP_EOL;
							$data_txt .= '※제품 문제 시 공정거래위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'.PHP_EOL.PHP_EOL;

							$data = array(
								$goods_info->GoodsCode.'.txt' => $data_txt
							);
							//debug($data);
							$this->zip->add_data($data);
						}

						//$this->goods_m->down_status($this->view_data['user_id'], $goods_info->goods_id, '', 'T');

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$this->zip->archive($this->config->item('user_temp_image_dir').'/danharoo_goods_txt.zip');

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}


	// 상품 선택 워터마크 만들기(이 함수 아래 if문은 아주 안좋아)
	function goods_select_watermark_make()
	{
		//$this->load->model('goods_m');
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		// thumbnail 생성
		$this->load->library('image_lib');

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE (1) AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();

					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();

						$watermark_home = $this->config->config['user_watermark_dir'];	// 절대경로
						$img_home = $this->config->config['user_temp_image_dir'].$goods_info->user_id;	// 절대경로
						//debug($img_home);

						$config['image_library'] = 'gd2';
						$config['wm_font_path'] = $watermark_home.'/NanumGothicExtraBold.ttf';
						$config['wm_vrt_alignment'] = 'top'; // 워터마크이미지의 세로 정렬을 설정합니다
						$config['wm_hor_alignment'] = 'left'; // 워터마크이미지의 가로 정렬을 설정합니다
						$config['maintain_ratio'] = TRUE;
						$config['quality'] = '100';	// 이미지의 품질을 설정
						$config['wm_opacity'] = '100';	// 이미지 투명도를 설정

						$config['source_image']	= $watermark_home.'/test.jpg';
						$config['wm_type'] = 'overlay';
						$config['wm_overlay_path']	= $watermark_home.'/jk1013_list1.jpg';
						$config['new_image']	= $watermark_home.'/test_w.jpg';
						//debug($config);
						$this->image_lib->initialize($config);
						//$this->image_lib->watermark();

						if ($this->image_lib->watermark())
						{
							$config['source_image']	= $watermark_home.'/test_w.jpg';
							$config['wm_type'] = 'overlay';
							$config['wm_hor_offset'] = '385'; // 워터마크이미지의 가로 정렬을 설정합니다
							$config['wm_vrt_offset'] = '423'; // 워터마크이미지의 가로 정렬을 설정합니다
							$config['wm_overlay_path']	= $watermark_home.'/B_P_05_5.jpg';
							$config['new_image']	= $watermark_home.'/test_w.jpg';
							$this->image_lib->initialize($config);

							if ($this->image_lib->watermark())
							{
								$config['source_image']	= $watermark_home.'/test_w.jpg';
								$config['wm_type'] = 'overlay';
								$config['wm_hor_offset'] = '0'; // 워터마크이미지의 가로 정렬을 설정합니다
								$config['wm_vrt_offset'] = '0'; // 워터마크이미지의 가로 정렬을 설정합니다
								$config['wm_overlay_path']	= $watermark_home.'/B_P_05_2.jpg';
								$config['new_image']	= $watermark_home.'/test_w.jpg';
								$this->image_lib->initialize($config);

								if ($this->image_lib->watermark())
								{
									$config['source_image']	= $watermark_home.'/test_w.jpg';
									$config['wm_text'] = '';
									$config['wm_type'] = 'text';
									$config['wm_font_size']	= '25';
									$config['wm_font_color'] = 'E70103';
									$config['wm_hor_offset'] = '610'; // 워터마크이미지의 가로 정렬을 설정합니다
									$config['wm_vrt_offset'] = '440'; // 워터마크이미지의 가로 정렬을 설정합니다
									$config['new_image']	= $watermark_home.'/test_w.jpg';
									$this->image_lib->initialize($config);

									if ($this->image_lib->watermark())
									{
										$config['source_image']	= $watermark_home.'/test_w.jpg';
										$config['wm_text'] = 'jk1013-코니카 트렌치 코트';
										$config['wm_type'] = 'text';
										$config['wm_font_size']	= '25';
										$config['wm_font_color'] = '000000';
										$config['wm_hor_offset'] = '120'; // 워터마크이미지의 가로 정렬을 설정합니다
										$config['wm_vrt_offset'] = '515'; // 워터마크이미지의 가로 정렬을 설정합니다
										$config['new_image']	= $watermark_home.'/test_w.jpg';
										$this->image_lib->initialize($config);

										if ($this->image_lib->watermark())
										{
											$config['source_image']	= $watermark_home.'/test_w.jpg';
											$config['wm_type'] = 'overlay';
											$config['wm_overlay_path']	= $watermark_home.'/B_N.jpg';
											$config['wm_hor_offset'] = '0'; // 워터마크이미지의 가로 정렬을 설정합니다
											$config['wm_vrt_offset'] = '490'; // 워터마크이미지의 가로 정렬을 설정합니다
											$config['new_image']	= $watermark_home.'/test_w.jpg';
											$this->image_lib->initialize($config);

											if ($this->image_lib->watermark())
											{
												$config['image_library'] = 'gd2';
												$config['source_image']	= $watermark_home.'/test_w.jpg';
												$config['wm_type'] = 'overlay';
												$config['wm_vrt_alignment'] = 'bottom'; // 워터마크이미지의 세로 정렬을 설정합니다
												$config['wm_hor_alignment'] = 'left'; // 워터마크이미지의 가로 정렬을 설정합니다
												$config['wm_overlay_path']	= $watermark_home.'/watermark.png';
												$config['maintain_ratio'] = TRUE;
												$config['new_image']	= $watermark_home.'/test_w.jpg';
												$this->image_lib->initialize($config);
												unset($config);

							/*
												if ($this->image_lib->watermark())
												{
													$config['image_library'] = 'gd2';
													$config['source_image']	= $watermark_home.'/test_w.jpg';
													$config['wm_type'] = 'overlay';
													$config['wm_vrt_alignment'] = 'bottom'; // 워터마크이미지의 세로 정렬을 설정합니다
													$config['wm_hor_alignment'] = 'center'; // 워터마크이미지의 가로 정렬을 설정합니다
													$config['wm_overlay_path']	= $watermark_home.'watermark.png';
													$config['maintain_ratio'] = TRUE;
													$config['new_image']	= $watermark_home.'/test_w.jpg';
													$this->image_lib->initialize($config);
													unset($config);

													if ($this->image_lib->watermark())
													{
														$config['image_library'] = 'gd2';
														$config['source_image']	= $watermark_home.'/test_w.jpg';
														$config['wm_type'] = 'overlay';
														$config['wm_vrt_alignment'] = 'bottom'; // 워터마크이미지의 세로 정렬을 설정합니다
														$config['wm_hor_alignment'] = 'right'; // 워터마크이미지의 가로 정렬을 설정합니다
														$config['wm_overlay_path']	= $watermark_home.'watermark.png';
														$config['maintain_ratio'] = TRUE;
														$config['new_image']	= $watermark_home.'/test_w.jpg';
														$this->image_lib->initialize($config);
														unset($config);

														if ($this->image_lib->watermark())
														{
															$config['image_library'] = 'gd2';
															$config['source_image']	= $watermark_home.'/test_w.jpg';
															$config['wm_type'] = 'overlay';
															$config['wm_vrt_alignment'] = 'bottom'; // 워터마크이미지의 세로 정렬을 설정합니다
															$config['wm_hor_alignment'] = 'right'; // 워터마크이미지의 가로 정렬을 설정합니다
															$config['wm_hor_offset'] = '100'; // 워터마크이미지의 가로 정렬을 설정합니다
															$config['wm_vrt_offset'] = '100'; // 워터마크이미지의 가로 정렬을 설정합니다
															$config['wm_overlay_path']	= $watermark_home.'watermark.png';
															$config['maintain_ratio'] = TRUE;
															$config['new_image']	= $watermark_home.'/test_w.jpg';
															$this->image_lib->initialize($config);
															unset($config);

															if ($this->image_lib->watermark())
															{
																$config['image_library'] = 'gd2';
																$config['source_image']	= $watermark_home.'/test_w.jpg';
																$config['wm_text'] = 'BW040-래쉬가드17';
																$config['wm_type'] = 'text';
																$config['wm_font_path'] = $watermark_home.'/D2Coding.ttc';
																$config['wm_font_size']	= '16';
																$config['wm_font_color'] = '286ABF';
																$config['wm_vrt_alignment'] = 'top'; // 워터마크이미지의 세로 정렬을 설정합니다
																$config['wm_hor_alignment'] = 'left'; // 워터마크이미지의 가로 정렬을 설정합니다
																$config['wm_hor_offset'] = '0'; // 워터마크이미지의 가로 정렬을 설정합니다
																$config['wm_vrt_offset'] = '0'; // 워터마크이미지의 가로 정렬을 설정합니다
																$config['wm_padding'] = '20';
																$config['maintain_ratio'] = TRUE;
																$config['new_image']	= $watermark_home.'/test_w.jpg';
																$this->image_lib->initialize($config);
																unset($config);

																if ($this->image_lib->watermark())
																{
																	$config['image_library'] = 'gd2';
																	$config['source_image']	= $watermark_home.'/test_w.jpg';
																	$config['wm_text'] = 'Copyright 2018 NEWTALK';
																	$config['wm_type'] = 'text';
																	$config['wm_font_path'] = $watermark_home.'/malgun_boot.ttf';
																	$config['wm_font_size']	= '16';
																	$config['wm_font_color'] = '000000';
																	$config['wm_vrt_alignment'] = 'top'; // 워터마크이미지의 세로 정렬을 설정합니다
																	$config['wm_hor_alignment'] = 'left'; // 워터마크이미지의 가로 정렬을 설정합니다
																	$config['wm_hor_offset'] = '50'; // 워터마크이미지의 가로 정렬을 설정합니다
																	$config['wm_vrt_offset'] = '50'; // 워터마크이미지의 가로 정렬을 설정합니다
																	$config['wm_padding'] = '20';
																	$config['maintain_ratio'] = TRUE;
																	$config['new_image']	= $watermark_home.'/test_w.jpg';
																	$this->image_lib->initialize($config);
																	unset($config);

																	if ($this->image_lib->watermark())
																	{
																		$config['image_library'] = 'gd2';
																		$config['source_image']	= $watermark_home.'/test_w.jpg';
																		$config['wm_text'] = 'Copyright 2018 NEWTALK';
																		$config['wm_type'] = 'text';
																		$config['wm_font_path'] = $watermark_home.'/malgun_boot.ttf';
																		$config['wm_font_size']	= '16';
																		$config['wm_font_color'] = '000000';
																		$config['wm_vrt_alignment'] = 'top'; // 워터마크이미지의 세로 정렬을 설정합니다
																		$config['wm_hor_alignment'] = 'left'; // 워터마크이미지의 가로 정렬을 설정합니다
																		$config['wm_hor_offset'] = '100'; // 워터마크이미지의 가로 정렬을 설정합니다
																		$config['wm_vrt_offset'] = '100'; // 워터마크이미지의 가로 정렬을 설정합니다
																		$config['wm_padding'] = '20';
																		$config['maintain_ratio'] = TRUE;
																		$config['new_image']	= $watermark_home.'/test_w.jpg';
																		$this->image_lib->initialize($config);
																		unset($config);

																		$this->image_lib->watermark();
																	}
																	else
																	{
																		echo $this->image_lib->display_errors();
																		break;
																	}
																}
																else
																{
																	echo $this->image_lib->display_errors();
																	break;
																}
															}
															else
															{
																echo $this->image_lib->display_errors();
																break;
															}
														}
														else
														{
															echo $this->image_lib->display_errors();
															break;
														}
													}
													else
													{
														echo $this->image_lib->display_errors();
														break;
													}
												}
												else
												{
													echo $this->image_lib->display_errors();
													break;
												}
							*/
											}
											else
											{
												echo $this->image_lib->display_errors();
												break;
											}
										}
										else
										{
											echo $this->image_lib->display_errors();
											break;
										}
									}
									else
									{
										echo $this->image_lib->display_errors();
										break;
									}
								}
								else
								{
									echo $this->image_lib->display_errors();
									break;
								}
							}
							else
							{
								echo $this->image_lib->display_errors();
								break;
							}
						}
						else
						{
							echo $this->image_lib->display_errors();
							break;
						}

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		//$this->zip->archive($this->config->item('user_temp_image_dir').'/danharoo_goods_txt.zip');

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 상품 선택 압축 다운
	function goods_select_zip_down()
	{
		if(@$this->uri->segment(3))
		{
			$user_id = $this->session->userdata('user_id');
			$gb = $this->uri->segment(3);

			$this->load->helper('download');	// 다운로드 헬퍼로드

			if($gb == 'T')
			{
				$data = file_get_contents($this->config->item('user_temp_image_dir').'/danharoo_goods_txt.zip'); // Read the file's contents
				$name = '단하루상품텍스트파일('.date('YmdHis', time()).').zip';

				//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');
			}
			elseif($gb == 'E')
			{
				//$data = file_get_contents($this->config->item('user_temp_image_dir').'/danharoo_goods_sabangnet_data.xls'); // Read the file's contents
				//$name = '사방넷등록엑셀파일('.date('YmdHis', time()).').xls';
			}
			elseif($gb == 'C')
			{
				//$data = file_get_contents($this->config->item('user_temp_image_dir').'/danharoo_goods_cafe24_data.csv'); // Read the file's contents
				//$name = '카페24등록CSV파일('.date('YmdHis', time()).').csv';
			}

			force_download($name, $data);
		}
		else
			alert_close('정상적인 접근이 아닙니다!');
	}

	// 상품 조회
	function goods_code_ajax_list()
	{
		$this->load->helper('directory');

		// thumbnail 생성
		$this->load->library('image_lib');

		//debug($_REQUEST);
		$user_id = 5;

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$where = "";

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;
				switch ($rule->op)
				{
					case "eq":
						$fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "") $whereArray[] = 'GSC.'.$fieldName.$fieldOperation;
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = "";
			//debug($where);
		}

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 상품목록
		$query = $this->db->query("SELECT count(GSC.id) AS cnt FROM goods_code AS GSC WHERE (1) {$where}");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$responce = new stdClass();

		$sql = "SELECT
					GSC.*,
					GS.GoodsName
					FROM
						goods_code	AS GSC LEFT OUTER JOIN
						goods		AS GS ON GSC.gcode = GS.GoodsCode
					WHERE
						(1) {$where}
					ORDER BY
						$sidx $sord
					LIMIT
						$start , $limit
		";
		//$query = $this->db->query("SELECT id, user_id, GoodsName, GoodsPrice, GoodsCount, GoodsImage, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				//if($row->SendCheck == 'Y') $SendCheck = '';
				$goods_img_dir = $this->config->item('user_goodscode_img_dir').$row->gcode.'/';
				$map = directory_map($goods_img_dir);
				$img_cnt = count($map['thumbnail']);

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													'',
													$row->gcode,
													$img_cnt,
													$row->created,
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}


	// 쿠팡온리 견적가 엑셀 다운로드
	function goods_select_coupang_tax_make()
	{
		$this->load->library('excel');

		$titleArray = array (
			'A' => array("공급처"),
			'B' => array("사입상품명"),
			'C' => array("상품코드"),
			'D' => array("카테고리(상품분류)"),
			'E' => array("카테고리 상품명"),
			'F' => array("상품 바코드"),
			'G' => array("검색어"),
			'H'=> array("브랜드"),
			'I' => array("컬러"),
			'J' => array("사이즈"),
			'K' => array("원가"),
			'L' => array("공급가"),
			'M' => array("공급가+vat"),
			'N' => array("무게"),
			'O' => array("소재"),
			'P' => array("제조국"),
			'Q' => array(" "),
			'R' => array("과세여부"),
			'S' => array("제조사"),
			'T' => array("거래타입"),
			'U' => array("수입여부"),
			'V' => array("박스 내 SKU 수량"),
			'W' => array("유통기간"),
			'X' => array("취급주의 사유"),
			'Y' => array("행어 입고"),
			'Z' => array("무게"),
			'AA' => array("포장 사이즈"),
			'AB' => array("출시 연도"),
			'AC' => array("계절"),
			'AD' => array("인증 마크 타입"),
			'AE' => array("인증 번호"),
			'AF' => array("고시명 "),
			'AG' => array("소재"),
			'AH' => array("컬러"),
			'AI' => array("사이즈"),
			'AJ' => array("제조자"),
			'AK' => array("제조국"),
			'AL' => array("주의사항"),
			'AM' => array("제조연월"),
			'AN' => array("품질 보증 기준"),
			'AO' => array("A/S 책임자와 번호 ")
		);

		
		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');

		// 임시
//		$goodsId = array('64086','64085');

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		foreach($titleArray AS $k => $v)
		{
			$headers[] = $v[0];
		}


		$objPHPExcel = PHPExcel_IOFactory::load('/home/danharoo/www/data/files/excel/coupang_tax_data.xls');
		$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);

		// 시트를 지정한다.
		$objPHPExcel->setActiveSheetIndex(0);
		$objPHPExcel->getActiveSheet()->setTitle('쿠팡온리 상품 리스트');

		// 첫 줄에 필드명을 기록한다.
		//$fields = $this->read_field_names();
		$col = 0;
		foreach ($headers as $title)
		{

			// 보더 컬러
			$border = [
				'borders' => [
					'allborders' => [
						'style' => PHPExcel_Style_Border::BORDER_THIN
					]
				]
			];		

			$objPHPExcel ->getActiveSheet()->setCellValueByColumnAndRow($col, 1, $title);
			$objPHPExcel->getActiveSheet()->getStyle('A1:AO1')->applyFromArray($border);
			$objPHPExcel->setActiveSheetIndex(0)->getStyle('A1:AO1')->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB("FFFFA07A");

			$col++;
		}
		// 실제 데이터 추출
		$column_row = 2;
		foreach($goodsId AS $k => $goods_id){
			$sql = "select GoodsEtc5,
							GoodsEtc6,
							(select concat(MiddleCategory, '>',SmallCategory) as category from goods_cate where id = substring_index(category3,'|',1) ) as category,
						   concat(category2 ,' > ', category3) as categoryCode,
						   GoodsName,
						   optionColor,
						   optionSize,
						   GoodsEtc24,
						   BrandName,       
						   GoodsEtc9,              
						   GoodsEtc17,
						   GoodsEtc16,
						   GoodsEtc21,
						   GoodsCode,
						   GoodsEtc37
					From goods where id = '". $goods_id ."' ";

			$result = $this->db->query($sql);
			$goods_info = $result->row();
			
			// 컬러 갯수에 맞게 row 확장
			$goodsColors = explode(",",$goods_info->optionColor);
			for($j = 0; $j < count($goodsColors); $j++){			
	
				// 셀 배열에 따른 값 삽입
				$column_col = 0;
				foreach($titleArray AS $k => $v)
				{
					$data = '';
					switch($k){
						case "A" : $data = $goods_info->GoodsEtc6; break;	// 공급처
						case "B": $data = $goods_info->GoodsEtc5; break;	// 사입상품명
						case "C": $data = $goods_info->GoodsCode; break; 	// 상품코드
						case "D": $data = $goods_info->category; break;	// 카테고리(상품분류)
						case "E": $data = $goods_info->GoodsEtc37." , ".$goodsColors[$j]." , ".$goods_info->optionSize; break;		// 카테고리 상품명
						case "G": $data = $goods_info->GoodsEtc24; break;		// 검색어
						case "H": $data = $goods_info->BrandName; break;		// 브랜드
						case "I": $data = $goodsColors[$j]; break;		// 컬러
						case "J": $data = $goods_info->optionSize; break;		// 사이즈
						case "K": $data = $goods_info->GoodsEtc9; break;		// 원가
						case "N": $data = $goods_info->GoodsEtc17; break;		// 무게
						case "O": $data = $goods_info->GoodsEtc16; break;		// 소재
						case "P": $data = ""; break;		// 공백
						case "Q": $data = $goods_info->GoodsEtc21; break;		// 제조국
						case "R": $data = '과세'; break;		// 과세여부
						case "S": $data = '(주)뉴톡'; break;		// 제조사
						case "T": $data = '조제사'; break;		// 제조사
						case "U": $data = ($goods_info->GoodsEtc21 == '대한민국') ? '수입대상아님' : '수입상품' ; break;		// 수입여부
						case "V": $data = '10'; break;		// 박스 내 SKU 수량
						case "W": $data = '0'; break;		// 유통기한
						case "X": $data = '해당사항없음'; break;		// 취급주의
						case "Y": $data = 'N'; break;		// 행어 입고
						case "Z": $data = $goods_info->GoodsEtc17; break;		// 무게
						case "AA": $data = '250*340*3'; break;		// 포장사이즈
						case "AB": $data = date('Y'); break;		// 출연 연도
						case "AC": $data = '사계절'; break;		// 계절
						case "AD": $data = '해당사항없음'; break;		// 인증 마크 타입
						case "AE": $data = '해당사항없음'; break;		// 인증 번호
						case "AF": $data = '의류'; break;		// 고시명
						case "AG": $data = $goods_info->GoodsEtc16; break;		// 소재#
						case "AH": $data = $goods_info->optionColor; break;		// 컬러#
						case "AI": $data = $goods_info->optionSize; break;		// 사이즈#
						case "AJ": $data = '(주)뉴톡'; break;		// 제조사
						case "AK": $data = $goods_info->GoodsEtc21; break;		// 제조국
						case "AL": $data = '단독세탁'; break;		// 제조국
						case "AM": $data = date('Y.m'); break;		// 제조연월
						case "AN": $data = '소비자보호 법에 준함 ※제품 문제시 공정거래 위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'; break;		// 품질 보증 기준
						case "AO": $data = '쿠팡 고객센터 1577-7011'; break;		// A/S책임자와 번호
						default: $data = "";
					}
					$objPHPExcel ->getActiveSheet()->setCellValueByColumnAndRow($column_col, $column_row, $data);
				$column_col++;
				}
				$column_row++;
			}
		
//			xecho($goods_info);
		}

		$writer = PHPExcel_IOFactory::createWriter($objPHPExcel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/coupang_tax_data.xls';
		$writer->save($rtn_file_path);
		
		echo '{"info":{"success":true,"text":"정상적으로 생성되었습니다."}}';
		exit;


	}

	// *****************************************
	// 쿠팡온리 상품 리스트 엑셀 다운로드
	// *****************************************
	function goods_select_coupang_list_make()
	{
		$this->load->library('excel');
			
		$titleArray = array (
			'A' => array("NO"),
			'B' => array("Style"),
			'C' => array("업체명"),
			'D' => array("모델명"),
			'E' => array("원가"),
			'F' => array("기타비용(라벨비등)"),
			'G' => array("원가(라벨비 추가)"),
			'H' => array("Style name"),
			'I' => array("제조국"),
			'J' => array("picture"),
			'K' => array("뉴톡희망가"),
			'L' => array("쿠팡희망가"),
			'M' => array("Final COGS"),
			'N' => array("Size"),
			'O' => array("Color"),
			'P' => array("Size QTY"),
			'Q' => array("Color QTY"),
			'R' => array("Order QTY per"),
			'S' => array("Order QTY"),
			'T' => array("Selling Point"),
			'U' => array("season"),
			'V' => array("비고"),
			'W' => array("쿠팡예정판가"),
			'X' => array("쿠팡수익율"),
			'Y' => array("COGS(-vat)수익"),
			'Z' => array("COGS(-vat)수익율"),
			'AA' => array("공급가1(17%)"),
			'AB' => array("공급가2(23%)"),
			'AC' => array("공급가3(29%)"),
			'AD' => array("공급가4(33%)"),
			'AE' => array("쿠팡판매가1"),
			'AF' => array("쿠팡판매가2"),
			'AG' => array("쿠팡판매가3"),
			'AH' => array("쿠팡판매가4")
		);

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');

		// 임시
//		$goodsId = array('64086','64349');

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		foreach($titleArray AS $k => $v)
		{
			$headers[] = $v[0];
		}


		$objPHPExcel = PHPExcel_IOFactory::load('/home/danharoo/www/data/files/excel/coupang_goods_list.xls');
		$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);

		// 시트를 지정한다.
		$objPHPExcel->setActiveSheetIndex(0);
		$objPHPExcel->getActiveSheet()->setTitle('쿠팡온리 상품 리스트');

		// 첫 줄에 필드명을 기록한다.
		//$fields = $this->read_field_names();
		$col = 0;
		foreach ($headers as $title)
		{
			// 보더 컬러
			$border = [
				'borders' => [
					'allborders' => [
						'style' => PHPExcel_Style_Border::BORDER_THIN
					]
				]
			];

			$objPHPExcel ->getActiveSheet()->setCellValueByColumnAndRow($col, 1, $title);
			$objPHPExcel->getActiveSheet()->getStyle('A1:AH1')->applyFromArray($border);
			$objPHPExcel->getActiveSheet()->getStyle('A2:AH2')->applyFromArray($border);
//			$objPHPExcel->getActiveSheet()->getColumnDimension($col)->setAutoSize(true);
			
//			if($col == 0){
				$objPHPExcel->getActiveSheet()->getColumnDimension('J')->setWidth(50);
				$objPHPExcel->setActiveSheetIndex(0)->getStyle('A1:J1')->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB("FFD3D3D3");
				$objPHPExcel->setActiveSheetIndex(0)->getStyle('K1:M1')->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB("FFFAFAD2");
				$objPHPExcel->setActiveSheetIndex(0)->getStyle('N1:S1')->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB("FFFFA07A");
				$objPHPExcel->setActiveSheetIndex(0)->getStyle('T1:V1')->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB("FF90EE90");
				$objPHPExcel->setActiveSheetIndex(0)->getStyle('W1:AH1')->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB("FF87CEFA");
//				$objPHPExcel->getActiveSheet()->getStyle('J0')->getFill()->setFillType(PHPExcel_Style_Fill::FILLL_SOLID)->getStartColor()->setARGB('FF0000');
//			}
			$col++;
		}
		// 실제 데이터 추출
		$column_row = 3;
		$No = 1;
		foreach($goodsId AS $k => $goods_id){
			$sql = "select GoodsEtc6, GoodsEtc5, GoodsEtc9, GoodsName, GoodsEtc21, optionSize, optionColor, GoodsEtc16, GoodsEtc37,  BrandName, GoodsCode_1,
					(select img_etc0 from goods_image where goods_id = goods.id limit 1 ) as img
				From goods where id = '". $goods_id ."' ";
			$result = $this->db->query($sql);
			$goods_info = $result->row();

			// 셀 배열에 따른 값 삽입
			$column_col = 0;				
			foreach($titleArray AS $k => $v)
			{
				$price = $goods_info->GoodsEtc9;
				switch($k){
					case "A" : $data = $No; break;	// NO
					case "B" : $data = strtoupper($goods_info->GoodsCode_1); break;	// 상품종류
					case "C" : $data = $goods_info->GoodsEtc6; break;	// 업체명
					case "D": $data = $goods_info->GoodsEtc5; break;	// 모델명(사입명)
					case "E": $data = $goods_info->GoodsEtc9; break; 	// 원가 
					case "H": $data = $goods_info->GoodsEtc37; break;	// Style name ( 입점몰 상품명 )
					case "I": $data = $goods_info->GoodsEtc21; break;		// 제조국(원산지)
//					case "J": $data = $goods_info->img; break;		// picture 이미지

					case "N": $data = $goods_info->optionSize; break;		// Size
					case "O": $data = $goods_info->optionColor; break;		// Color
//					case "Y": $data = $goods_info->GoodsEtc16; break;		// FABRIC(소재)
//					case "AD": $data = $price * 1.2; break;		// 공급가1(17%)
//					case "AE": $data = $price * 1.3; break;		// 공급가2(23%)
//					case "AF": $data = $price * 1.4; break;		// 공급가3(29%)
//					case "AG": $data = $price * 1.5; break;		// 공급가4(33%)
//					case "AH": $data = ($price * 1.2) * 2.1; break;		// 쿠팡판매가1
//					case "AI": $data = ($price * 1.3) * 2.1; break;		// 쿠팡판매가2
//					case "AJ": $data = ($price * 1.4) * 2.1; break;		// 쿠팡판매가3
//					case "AK": $data = ($price * 1.5) * 2.1; break;		// 쿠팡판매가4
					default: $data = "";
				}


				$imgFile = "/home/danharoo/www/data/files/goods/5/".$goods_info->img; // 이미지 경로
				if($k == 'J' &&  file_exists($imgFile) && $goods_info->img != ''){
					$imgInfo = GetImageSize($imgFile); // 깨진 이미지 파일 때문에 이미지 너비 사이즈 1000 이상일 경우에만 엑셀에 삽입 ( 정상 이미지 아닐 경우 엑셀 자체 오류 발생 )
					if($imgInfo[0] > 1000){
////						echo $goods_id . "-".$column_row."<img src='/data/files/goods/5/".$goods_info->img."' width=50>".$goods_info->img."<br>";
						$iCol = 'J'; // 컬럼번호
						$iRow = $column_row; // 행번호
						$photo_path = $imgFile;
						$objDrawing = new PHPExcel_Worksheet_Drawing();
						$objDrawing->setName('Photo '.$iRow);
						$objDrawing->setDescription('Photo '.$iRow);
						$objDrawing->setPath($photo_path);
						$objDrawing->setResizeProportional(true);						
//						$objDrawing->setWidth(35);
						$objDrawing->setHeight(240);
						$objDrawing->setOffsetX(2);
						$objDrawing->setOffsetY(2);
						$objDrawing->setCoordinates($iCol.$iRow);
						$objDrawing->setWorksheet($objPHPExcel->getActiveSheet());
						$objPHPExcel->getActiveSheet()->getRowDimension($iRow)->setRowHeight(210); // 행높이 설정
					}
				}else{
					$objPHPExcel ->getActiveSheet()->setCellValueByColumnAndRow($column_col, $column_row, $data);
				}
			$column_col++;			
			}
		$No++;
		$column_row++;
//			xecho($goods_info);
		}

		$writer = PHPExcel_IOFactory::createWriter($objPHPExcel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/coupang_goods_list.xls';
		$writer->save($rtn_file_path);
		
		echo '{"info":{"success":true,"text":"정상적으로 생성되었습니다."}}';
		exit;
	}

	// 상품 선택 샤방넷 엑셀 만들기
	function goods_select_excel_make()
	{
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->load->library('excel');

		$excel_data = $this->config->item('excel_data');
		//debug($excel_width_data);

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;
	

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		foreach($excel_data AS $k => $v)
		{
			$headers[] = $v[0];
		}
		//debug($headers);

		// 시트를 지정한다.
		$this->excel->setActiveSheetIndex(0);
		$this->excel->getActiveSheet()->setTitle('단하루 상품');

		// 첫 줄에 필드명을 기록한다.
		//$fields = $this->read_field_names();
		$col = 0;
		foreach ($headers as $title)
		{
			$this->excel->getActiveSheet()->setCellValueByColumnAndRow($col, 1, $title);
			$col++;
		}

		$row = 2;
		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인 (SQL Injection 수정 2026-02-02 Luna)
				$query1 = $this->db->select('id')
				                   ->from('goods_master')
				                   ->where('user_id', $user_id)
				                   ->where('id', $goods_id)
				                   ->get();
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인 (SQL Injection 수정 2026-02-02 Luna)
					$query2 = $this->db->select('GS.*, GSD.*, GSI.*, US.sabangnet_id')
					                   ->from('goods AS GS')
					                   ->join('goods_detail AS GSD', 'GS.id = GSD.goods_id', 'left outer')
					                   ->join('goods_image AS GSI', 'GS.id = GSI.goods_id', 'left outer')
					                   ->join('users AS US', 'GS.GoodsEtc6 = US.username', 'left outer')
					                   ->where('GS.GdsMstId', $goods_id)
					                   ->where('auth_code', '4')
					                   ->get();
					// var_dump($sql12);die();
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();
						//debug($this->goods_excel_data($query2->row()));
                        // if($_SERVER["REMOTE_ADDR"] == "218.157.131.10") {
                        //     $created = str_replace('-', '', substr($goods_info->created, 0, 10));
                        //     debug($created);exit;
                        // }

						$records = $this->goods_excel_data($goods_info);
						//debug($record);
						$col = 0;
						foreach ($records as $record)
						{
							$this->excel->getActiveSheet()->setCellValueByColumnAndRow($col, $row, $record);
							$col++;
						}

						$data_txt = '';
						if($goods_info->GoodsName)
						{
							$data_txt .= '[ '.$goods_info->GoodsName.' ]'.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ''.$goods_info->Description.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= 'Product Info_'.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - FABRIC(소재) : '.$goods_info->GoodsEtc16.PHP_EOL.PHP_EOL;
							$data_txt .= ' - COLOR(색상) : '.$goods_info->OptionColor.PHP_EOL.PHP_EOL;
							$data_txt .= ' - 원단느낌 : '.$goods_info->GoodsEtc15.PHP_EOL.PHP_EOL;
							$data_txt .= ' - WASHING(세탁방법) : '.$goods_info->GoodsEtc18.PHP_EOL.PHP_EOL;
							$data_txt .= ' - Weight(무게:g) : '.$goods_info->GoodsEtc17.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SIZE(사이즈) : '.$goods_info->OptionSize.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SizeSpec(상세사이즈) : '.$goods_info->GoodsEtc14.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조사 : '.$goods_info->MakerName.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조일 : '.$goods_info->GoodsEtc20.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조국 : '.$goods_info->GoodsEtc21.PHP_EOL.PHP_EOL;
							$data_txt .= '- 품질보증기준 : '.$goods_info->GoodsEtc22.PHP_EOL.PHP_EOL;
							$data_txt .= '※제품 문제 시 공정거래위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'.PHP_EOL.PHP_EOL;

							$data = array(
								$goods_info->GoodsCode.'.txt' => $data_txt
							);
							//debug($data);
							$this->zip->add_data($data);
						}

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$this->zip->archive($this->config->item('user_temp_image_dir').'/danharoo_goods_txt.zip');

		$writer = PHPExcel_IOFactory::createWriter($this->excel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/danharoo_goods_data.xls';
		
		$writer->save($rtn_file_path);

		//$this->zip->read_file($rtn_file_path);
		//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 상품 전체(조건 - 노출 : 'Y', 상품상태 : '공급중') 샤방넷 엑셀 만들기(2018.10.10)
	function goods_all_excel_make()
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->load->library('excel');

		$excel_data = $this->config->item('excel_data');
		//debug($excel_width_data);

		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		foreach($excel_data AS $k => $v)
		{
			$headers[] = $v[0];
		}
		//debug($headers);

		// 시트를 지정한다.
		$this->excel->setActiveSheetIndex(0);
		$this->excel->getActiveSheet()->setTitle('단하루 상품');

		// 첫 줄에 필드명을 기록한다.
		//$fields = $this->read_field_names();
		$col = 0;
		foreach ($headers as $title)
		{
			$this->excel->getActiveSheet()->setCellValueByColumnAndRow($col, 1, $title);
			$col++;
		}

		$sql = " SELECT GdsMstId FROM goods WHERE user_id='{$user_id}' and activated = 'Y' AND GoodsEtc52 = '2' ORDER BY created desc ";
        //debug($sql);
		$query = $this->db->query($sql);

		$row = 2;
		foreach($query->result() as $a_row)
		{
			$goods_id = $a_row->GdsMstId;
			//debug($goods_id);

			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();
						//debug($this->goods_excel_data($query2->row()));
						$records = $this->goods_excel_data($goods_info);
						//debug($record);
						$col = 0;
						foreach ($records as $record)
						{
							$this->excel->getActiveSheet()->setCellValueByColumnAndRow($col, $row, $record);
							$col++;
						}

						$data_txt = '';
						if($goods_info->GoodsName)
						{
							$data_txt .= '[ '.$goods_info->GoodsName.' ]'.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ''.$goods_info->Description.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= 'Product Info_'.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - FABRIC(소재) : '.$goods_info->GoodsEtc16.PHP_EOL.PHP_EOL;
							$data_txt .= ' - COLOR(색상) : '.$goods_info->OptionColor.PHP_EOL.PHP_EOL;
							$data_txt .= ' - 원단느낌 : '.$goods_info->GoodsEtc15.PHP_EOL.PHP_EOL;
							$data_txt .= ' - WASHING(세탁방법) : '.$goods_info->GoodsEtc18.PHP_EOL.PHP_EOL;
							$data_txt .= ' - Weight(무게:g) : '.$goods_info->GoodsEtc17.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SIZE(사이즈) : '.$goods_info->OptionSize.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SizeSpec(상세사이즈) : '.$goods_info->GoodsEtc14.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조사 : '.$goods_info->MakerName.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조일 : '.$goods_info->GoodsEtc20.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조국 : '.$goods_info->GoodsEtc21.PHP_EOL.PHP_EOL;
							$data_txt .= '- 품질보증기준 : '.$goods_info->GoodsEtc22.PHP_EOL.PHP_EOL;
							$data_txt .= '※제품 문제 시 공정거래위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'.PHP_EOL.PHP_EOL;

							$data = array(
								$goods_info->GoodsCode.'.txt' => $data_txt
							);
							//debug($data);
							$this->zip->add_data($data);
						}

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$this->zip->archive($this->config->item('user_temp_image_dir').'/danharoo_goods_txt.zip');

		$writer = PHPExcel_IOFactory::createWriter($this->excel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/danharoo_goods_data.xls';
		$writer->save($rtn_file_path);

		//$this->zip->read_file($rtn_file_path);
		//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');

		echo '{"info":{"success":true,"text":"전체 상품이 처리되었습니다."}}';
	}

	// 상품 전체(조건 - 노출 : 'Y', 상품상태 : '공급중') 샤방넷 엑셀 만들기(2018.10.12)
	function goods_all_excel_make_search()
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->load->library('excel');

		$excel_data = $this->config->item('excel_data');
		//debug($excel_width_data);

		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		foreach($excel_data AS $k => $v)
		{
			$headers[] = $v[0];
		}
		//debug($headers);

		// 시트를 지정한다.
		$this->excel->setActiveSheetIndex(0);
		$this->excel->getActiveSheet()->setTitle('단하루 상품');

		// 첫 줄에 필드명을 기록한다.
		//$fields = $this->read_field_names();
		$col = 0;
		foreach ($headers as $title)
		{
			$this->excel->getActiveSheet()->setCellValueByColumnAndRow($col, 1, $title);
			$col++;
		}

		$where = "";

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$cate1 = $this->input->post('cate1');
		$cate2 = $this->input->post('cate2');
		$cate3 = $this->input->post('cate3');

		$sCreated = $this->input->post('sCreated');
		$eCreated = $this->input->post('eCreated');

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;

				if($fieldName == 'GoodsName') $fieldData = strtoupper($fieldData);
				if($fieldName == 'GoodsCode') $fieldData = strtolower($fieldData);
                if($fieldName == 'modified') {
                    $fieldName = 'GS.modified';
                    //debug($fieldName);
                }

				switch ($rule->op)
				{
					case "eq":
						$fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "") $whereArray[] = $fieldName.$fieldOperation;
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = "";
			//debug($where);
		}

		if($cate1)
		{
			$where .= " and Category1='$cate1' ";

			if($cate2)
			{
				$where .= " and Category2='$cate2' ";

				if($cate3)
					$where .= " and Category3='$cate3' ";
			}
		}

		if($sCreated && !$eCreated)
			$where .= " and created >= '$sCreated' ";
		if($sCreated && $eCreated)
			$where .= " and created BETWEEN '$sCreated 00:00:00' AND '$eCreated 23:59:59' ";
			//debug($where);

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$sql = "SELECT
					GdsMstId
					FROM
						goods
					WHERE
						user_id='{$user_id}' {$where}
					ORDER BY
						$sidx $sord
		";
		//debug($sql);exit;
		$query = $this->db->query($sql);

		$row = 2;
		foreach($query->result() as $a_row)
		{
			$goods_id = $a_row->GdsMstId;
			//debug($goods_id);

			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();
						//debug($this->goods_excel_data($query2->row()));
						$records = $this->goods_excel_data($goods_info);
						//debug($record);
						$col = 0;
						foreach ($records as $record)
						{
							$this->excel->getActiveSheet()->setCellValueByColumnAndRow($col, $row, $record);
							$col++;
						}

						$data_txt = '';
						if($goods_info->GoodsName)
						{
							$data_txt .= '[ '.$goods_info->GoodsName.' ]'.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ''.$goods_info->Description.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= 'Product Info_'.PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - FABRIC(소재) : '.$goods_info->GoodsEtc16.PHP_EOL.PHP_EOL;
							$data_txt .= ' - COLOR(색상) : '.$goods_info->OptionColor.PHP_EOL.PHP_EOL;
							$data_txt .= ' - 원단느낌 : '.$goods_info->GoodsEtc15.PHP_EOL.PHP_EOL;
							$data_txt .= ' - WASHING(세탁방법) : '.$goods_info->GoodsEtc18.PHP_EOL.PHP_EOL;
							$data_txt .= ' - Weight(무게:g) : '.$goods_info->GoodsEtc17.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SIZE(사이즈) : '.$goods_info->OptionSize.PHP_EOL.PHP_EOL;
							$data_txt .= ' - SizeSpec(상세사이즈) : '.$goods_info->GoodsEtc14.PHP_EOL.PHP_EOL;
							$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조사 : '.$goods_info->MakerName.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조일 : '.$goods_info->GoodsEtc20.PHP_EOL.PHP_EOL;
							$data_txt .= '- 제조국 : '.$goods_info->GoodsEtc21.PHP_EOL.PHP_EOL;
							$data_txt .= '- 품질보증기준 : '.$goods_info->GoodsEtc22.PHP_EOL.PHP_EOL;
							$data_txt .= '※제품 문제 시 공정거래위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'.PHP_EOL.PHP_EOL;

							$data = array(
								$goods_info->GoodsCode.'.txt' => $data_txt
							);
							//debug($data);
							$this->zip->add_data($data);
						}

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$this->zip->archive($this->config->item('user_temp_image_dir').'/danharoo_goods_txt.zip');

		$writer = PHPExcel_IOFactory::createWriter($this->excel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/danharoo_goods_data.xls';
		$writer->save($rtn_file_path);

		//$this->zip->read_file($rtn_file_path);
		//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');

		echo '{"info":{"success":true,"text":"전체 상품이 처리되었습니다."}}';
	}

	// 상품 선택 샤방넷 엑셀 압축 다운
	function goods_select_excel_zip_down()
	{
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->zip->read_file($this->config->item('user_temp_image_dir').'/danharoo_goods_txt.zip');
		$this->zip->read_file($this->config->item('user_temp_image_dir').'/danharoo_goods_data.xls');

		$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');
	}

	// 검색 상품 전체 뉴톡수정엑셀파일 만들기(2019.12.20)
	function goods_search_newtalk_excel_make()
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->load->library('excel');

		$user_id = $this->session->userdata('user_id');

		// 상품관리자 추가(2020.06.06)
		if($this->view_data['auth_code'] == 12)
		{
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		$where = "";

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$cate1 = $this->input->post('cate1');
		$cate2 = $this->input->post('cate2');
		$cate3 = $this->input->post('cate3');

		$sCreated = $this->input->post('sCreated');
		$eCreated = $this->input->post('eCreated');

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;

				if($fieldName == 'GoodsName') $fieldData = strtoupper($fieldData);
				if($fieldName == 'GoodsCode') $fieldData = strtolower($fieldData);
                if($fieldName == 'modified') {
                    $fieldName = 'GS.modified';
                    //debug($fieldName);
                }

				switch ($rule->op)
				{
					case "eq":
						$fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "") $whereArray[] = $fieldName.$fieldOperation;
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = "";
			//debug($where);
		}

		if($cate1)
		{
			$where .= " and Category1='$cate1' ";

			if($cate2)
			{
				$where .= " and Category2='$cate2' ";

				if($cate3)
					$where .= " and Category3='$cate3' ";
			}
		}

		if($sCreated && !$eCreated)
			$where .= " and created >= '$sCreated' ";
		if($sCreated && $eCreated)
			$where .= " and created BETWEEN '$sCreated 00:00:00' AND '$eCreated 23:59:59' ";
			//debug($where);

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$sql = "SELECT
					GdsMstId
					FROM
						goods
					WHERE
						user_id='{$user_id}' {$where}
					ORDER BY
						$sidx $sord
		";
		//debug($sql);exit;
		$query = $this->db->query($sql);

		// 엑셀 자료 생성
		$real_excel_data = [];	// 사방넷
		$headers = [];			// 사방넷
		//debug(count($headers));

        // 뉴톡 엑셀 샘플파일(2019.12.20) 로드
		$objPHPExcel = PHPExcel_IOFactory::load('/home/danharoo/www/data/files/excel/danharoo_newtalk.xls');
		$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
		//debug($sheetDataArr);

		// 시트를 지정한다.
		$objPHPExcel->setActiveSheetIndex(0);
		$objPHPExcel->getActiveSheet()->setTitle('뉴톡수정엑셀상품');

		$row = 2;
		foreach($query->result() as $a_row)
		{
			$goods_id = $a_row->GdsMstId;
			//debug($goods_id);

			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE (1) AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();
						//debug($this->goods_excel_data($query2->row()));

						// 상품분류 (대>중>소>세)
						$goods_cate = '';
						$goods_cate1 = $goods_info->Category1;
						$goods_cate2_arr = explode('|', $goods_info->Category2);
						$goods_cate3_arr = explode('|', $goods_info->Category3);
						if($goods_cate1)
						{
							$query3 = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate1}");
							if($query3->num_rows() > 0) $cate1 = $query3->row();

							if($cate1->LargeCode)
							{
								$goods_info->Category1 = $cate1->LargeCategory;

								if($goods_cate2_arr[0])
								{
									$query4 = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate2_arr[0]}");
									if($query4->num_rows() > 0) $cate2 = $query4->row();

									if($cate2->MiddleCode)
									{
										$goods_info->Category2 = $cate2->MiddleCategory;

										if($goods_cate3_arr[0])
										{
											$query5 = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate3_arr[0]}");
											if($query5->num_rows() > 0) $cate3 = $query5->row();

											if($cate3->SmallCode)
												$goods_info->Category3 = $cate3->SmallCategory;
										}
									}
								}
							}
						}

						$records = $this->goods_excel_newtalk_data($goods_info);	// 뉴톡
						//debug($record);
						$col = 0;
						foreach ($records as $record)
						{
							$objPHPExcel->getActiveSheet()->setCellValueByColumnAndRow($col, $row, $record);
							$col++;
						}

						// $this->goods_m->down_status($this->view_data['user_id'], $goods_info->goods_id, '', 'D');

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$writer = PHPExcel_IOFactory::createWriter($objPHPExcel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/danharoo_goods_newtalk_data.xls';
		$writer->save($rtn_file_path);

		//$this->zip->read_file($rtn_file_path);
		//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');

		echo '{"info":{"success":true,"text":"검색 상품이 처리되었습니다."}}';
	}

	// JSZip용 이미지 URL 목록 반환
	function goods_zip_urls()
	{
		if($this->view_data['down_level'] < 1) {
			header('Content-Type: application/json; charset=utf-8');
			echo json_encode(['success' => false, 'msg' => '다운로드 권한이 없습니다.']);
			return;
		}
		$goods_code = $this->input->get('code');
		if(!$goods_code) {
			header('Content-Type: application/json; charset=utf-8');
			echo json_encode(['success' => false, 'msg' => '잘못된 접근입니다.']);
			return;
		}
			$img_dir  = $this->config->item('user_goodscode_img_dir') . $goods_code . '/';
			$cdn_base = 'https://newtalk.kr/data/files/goods/goodscode/img/' . $goods_code . '/';
			$names = $this->_goodscode_image_files($goods_code);
			$urls  = [];
			foreach ($names as $name) {
				$urls[] = [
					'url' => $cdn_base . rawurlencode($name),
					'fallback_url' => '/products/goods_zip_file?code=' . rawurlencode($goods_code) . '&file=' . rawurlencode($name),
					'zip_path' => $goods_code . '/' . $name
				];
			}
		$sql = "SELECT GS.*, GSD.* FROM goods AS GS
				LEFT OUTER JOIN goods_detail AS GSD ON GS.id = GSD.goods_id
				WHERE GS.GoodsCode='{$goods_code}'";
		$goods_info = $this->db->query($sql)->row();
		$txt = '';
		$goods_name = $goods_code;
		if ($goods_info && $goods_info->GoodsName) {
			if ($goods_info->GoodsEtc5) $goods_info->GoodsName = $goods_info->GoodsEtc5;
			$goods_name = $goods_info->GoodsName;
			$txt .= '[ ' . $goods_info->GoodsName . ' ]' . PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= $goods_info->Description . PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= 'Product Info_' . PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= ' - FABRIC(소재) : ' . $goods_info->GoodsEtc16 . PHP_EOL.PHP_EOL;
			$txt .= ' - COLOR(색상) : ' . $goods_info->OptionColor . PHP_EOL.PHP_EOL;
			$txt .= ' - WASHING(세탁방법) : ' . $goods_info->GoodsEtc18 . PHP_EOL.PHP_EOL;
			$txt .= ' - Weight(무게:g) : ' . $goods_info->GoodsEtc17 . PHP_EOL.PHP_EOL;
			$txt .= ' - SIZE(사이즈) : ' . $goods_info->OptionSize . PHP_EOL.PHP_EOL;
			$txt .= ' - SizeSpec(상세사이즈) : ' . $goods_info->GoodsEtc14 . PHP_EOL.PHP_EOL;
		}
		header('Content-Type: application/json; charset=utf-8');
		echo json_encode([
			'success'    => true,
			'goods_code' => $goods_code,
				'goods_name' => $goods_name,
				'zip_name'   => $goods_name . '.zip',
				'partial_zip_name' => $goods_name . '_partial_missing.zip',
				'expected_count' => count($urls),
				'txt'        => $txt,
				'txt_name'   => $goods_code . '.txt',
				'images'     => $urls,
			], JSON_UNESCAPED_UNICODE);
		}

		function goods_zip_download_log()
		{
			$goods_code = $this->input->post('code');
			$expected = intval($this->input->post('expected'));
			$success = intval($this->input->post('success'));
			$failed = $this->input->post('failed');
			$user_id = $this->session->userdata('user_id') ? $this->session->userdata('user_id') : '0';
			$log = array(
				'type' => 'jszip_partial_download',
				'user_id' => $user_id,
				'goods_code' => $goods_code,
				'expected' => $expected,
				'success' => $success,
				'failed' => json_decode($failed, true),
				'ip' => $this->input->ip_address(),
				'ua' => substr((string)$this->input->user_agent(), 0, 200),
			);
			log_message('error', '[goods_zip_download] ' . json_encode($log, JSON_UNESCAPED_UNICODE));
			header('Content-Type: application/json; charset=utf-8');
			echo json_encode(array('success' => true));
		}

		function goods_zip_file()
		{
			if($this->view_data['down_level'] < 1) {
				show_404();
				return;
			}

			$goods_code = preg_replace('/[^a-zA-Z0-9_-]/', '', (string)$this->input->get('code'));
			$file = basename((string)$this->input->get('file'));
			if(!$goods_code || !$file || !in_array($file, $this->_goodscode_image_files($goods_code))) {
				show_404();
				return;
			}

			$file_path = $this->config->item('user_goodscode_img_dir') . $goods_code . '/' . $file;
			if(!is_file($file_path)) {
				show_404();
				return;
			}

			$mime = function_exists('mime_content_type') ? mime_content_type($file_path) : 'application/octet-stream';
			header('Content-Type: ' . ($mime ? $mime : 'application/octet-stream'));
			header('Content-Length: ' . filesize($file_path));
			header('Cache-Control: private, no-store, max-age=0');
			header('X-NewTalk-Download-Fallback: 1');
			readfile($file_path);
		}

		// 상품코드 이미지 압축다운
	function goods_code_zip_down()
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->model('goods_m');

		if($this->view_data['down_level'] < 1)
			alert('다운로드 권한이 없습니다!');

		if(!$this->uri->segment(3))
			alert('정상적인 접근이 아닙니다!');

		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$goods_code = $this->uri->segment(3);

		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
		
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.id AS goods_pk,
						GS.*,
						GSD.*,
						GSI.gi_id, GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9,
					GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14, GSI.img15, GSI.img16, GSI.img17, GSI.img18, GSI.img19,
					GSI.img_etc0, GSI.img_etc1, GSI.img_etc2, GSI.img_etc3, GSI.img_etc4, GSI.img_etc5, GSI.img_etc6, GSI.img_etc7,
					GSI.img_etc8, GSI.img_etc9, GSI.modified
					FROM
						goods AS		GS LEFT OUTER JOIN
						goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
						goods_image		As GSI ON GS.id = GSI.goods_id
					WHERE GS.GoodsCode='{$goods_code}'
		";
			$query = $this->db->query($sql);
			$goods_info = $query->row();
			if(!$goods_info)
				alert('해당 상품 정보가 존재하지 않습니다!');
			$download_goods_id = (isset($goods_info->goods_pk) && $goods_info->goods_pk) ? $goods_info->goods_pk : $goods_info->id;
			//debug($goods_info);

		// 엑셀 데이타 처리
		//$this->goods_excel_data($goods_info);
		//$this->zip->read_file($this->config->item('user_temp_image_dir').$this->view_data['user_id'].'/'.$goods_code.'.xls');
		//$this->zip->read_file($this->goods_excel_data($goods_info));

		$data_txt = '';
		if($goods_info->GoodsName)
		{
			$data_txt .= '[ '.$goods_info->GoodsName.' ]'.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ''.$goods_info->Description.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= 'Product Info_'.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ' - FABRIC(소재) : '.$goods_info->GoodsEtc16.PHP_EOL.PHP_EOL;
			$data_txt .= ' - COLOR(색상) : '.$goods_info->OptionColor.PHP_EOL.PHP_EOL;
			$data_txt .= ' - 원단느낌 : '.$goods_info->GoodsEtc15.PHP_EOL.PHP_EOL;
			$data_txt .= ' - WASHING(세탁방법) : '.$goods_info->GoodsEtc18.PHP_EOL.PHP_EOL;
			$data_txt .= ' - Weight(무게:g) : '.$goods_info->GoodsEtc17.PHP_EOL.PHP_EOL;
			$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ' - SIZE(사이즈) : '.$goods_info->OptionSize.PHP_EOL.PHP_EOL;
			$data_txt .= ' - SizeSpec(상세사이즈) : '.$goods_info->GoodsEtc14.PHP_EOL.PHP_EOL;
			$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조사 : '.$goods_info->MakerName.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조일 : '.$goods_info->GoodsEtc20.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조국 : '.$goods_info->GoodsEtc21.PHP_EOL.PHP_EOL;
			$data_txt .= '- 품질보증기준 : '.$goods_info->GoodsEtc22.PHP_EOL.PHP_EOL;
			$data_txt .= '※제품 문제 시 공정거래위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'.PHP_EOL.PHP_EOL;

			$data = array(
                $goods_code.'.txt' => $data_txt
            );
			//debug($data);
			$this->zip->add_data($data);
		}

// 		$this->zip->read_dir($goods_img_dir, FALSE);


		// ===== 2026-01-29: thumbnail 폴더 완전 차단 START (v2) =====
		if (is_dir($goods_img_dir)) {
			$files = scandir($goods_img_dir);
			foreach ($files as $file) {
					if ($file == '.' || $file == '..') continue;
					$file_path = $goods_img_dir . $file;

					// 모든 디렉토리 완전 차단 (thumbnail 포함)
					if (is_dir($file_path)) {
						continue; // 모든 하위 디렉토리 무시
					}

					// thumbnail 파일명 패턴 차단 (-s, -m, -l, _thumb)
					if (is_dir($file_path)) {
						continue;
					}

					// 원본 파일만 ZIP에 추가 (파일만, 디렉토리 제외)
					if (is_file($file_path)) {
						$this->zip->read_file($file_path, FALSE);
				}
			}
			}
			// ===== 2026-01-29: thumbnail 폴더 완전 차단 END (v2) =====

			$this->goods_m->down_status($this->view_data['user_id'], $download_goods_id, $goods_code);

		$this->zip->download('danharoo.'.$goods_code.'.zip');

		alert_close('다운로드가 완료되었습니다!');
	}

	// 선택 상품 상품코드 이미지 압축(2021.08.21)
	function goods_code_select_down()
	{
		ini_set('memory_limit','-1');

		$goodsIds = $this->input->post('goodsIds');
		//debug($gb);exit;

		if(!$goodsIds)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$setting_cnt = count($goodsIds); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsIds AS $k => $goodsId)
		{
			if($goodsId)
			{
				// 등록 상품 정보 확인
				$sql = "SELECT GS.GoodsCode	FROM goods AS GS WHERE GS.id='{$goodsId}'";
				$query = $this->db->query($sql);

				if($query->num_rows() > 0)
				{
					foreach($query->result() as $row)
					{
						if($row->GoodsCode)
						{
				            $goods_img_dir = $this->config->item('user_goodscode_img_dir').$row->GoodsCode.'/';
//                             $this->zip->read_dir($goods_img_dir, FALSE);


							// ===== 2026-01-29 v7: thumbnail 폴더만 제외 (파일명 패턴 제거) =====
							// CEO 확인: -s_1.jpg는 원본 이미지 (4:3 비율, 연번 1)
							$files = scandir($goods_img_dir);
							foreach ($files as $file) {
									if ($file === '.' || $file === '..') continue;
									$file_path = $goods_img_dir . '/' . $file;

									// ✅ 디렉토리만 제외 (thumbnail 폴더 등)
									if (is_dir($file_path)) {
										continue;
									}

									// ✅ 모든 파일 추가 (원본 이미지) — GoodsCode 폴더로 정리
									if (is_file($file_path)) {
										$img_data = file_get_contents($file_path);
										if ($img_data !== FALSE) {
											$this->zip->add_data($row->GoodsCode . '/'. $file, $img_data);
										}
									}
								}
							// ===== v7 END =====
                            $success_cnt++; // 성공 시 카운트
                        }
					}
				}
			}
		}

			$zip_file = $this->_download_unique_zip_filename('danharoo_goods_code_img');
			$zip_cache_file = $this->config->item('user_temp_image_dir').'/'.$zip_file;
			if(!$this->zip->archive($zip_cache_file))
			{
				echo json_encode(array('info' => array('success' => false, 'text' => '다운로드 파일 생성에 실패했습니다.')), JSON_UNESCAPED_UNICODE);
				exit;
			}

			//$this->zip->download('autoscrap.goods.'.time().'.zip');
			//alert_close('다운로드가 완료되었습니다!');
			echo json_encode(array('info' => array('success' => true, 'text' => '선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다.', 'file' => $zip_file)), JSON_UNESCAPED_UNICODE);
		}

	// 선택 상품 상품 선택 압축 다운(2021.08.21)
	function goods_code_select_zip_down()
	{
		ini_set('memory_limit','-1');

		$this->load->library('zip');

	        $user_id = $this->session->userdata('user_id');

	        $this->load->helper('download');	// 다운로드 헬퍼로드

	        $zip_file = $this->_download_request_zip_filename('danharoo_goods_code_img', 'danharoo_goods_code_img.zip');
	        $zip_path = $this->config->item('user_temp_image_dir').'/'.$zip_file;
	        if(!is_file($zip_path))
				alert_close('다운로드 파일이 존재하지 않습니다!');

	        $this->zip->read_file($zip_path);
	        $name = urlencode('뉴톡상품이미지파일').'('.date('YmdHis', time()).').zip';

	        $this->zip->download($name);
	}

	// 상품 엑셀 데이타 처리
	function goods_excel_data($goods_info)
	{
		//debug($goods_info);
		$excel_data = $this->config->item('excel_data');
		//debug($excel_width_data);

		$rtn_file_path = '';

		// 상품분류 (대>중>소>세)
		$goods_cate = '';
		$goods_cate1 = $goods_info->Category1;
		$goods_cate2_arr = explode('|', $goods_info->Category2);
		$goods_cate3_arr = explode('|', $goods_info->Category3);
		if($goods_cate1)
		{
			$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate1}");
			if($query->num_rows() > 0) $cate1 = $query->row();

			if($cate1->LargeCode)
			{
				$goods_cate .= $cate1->LargeCode;

				if($goods_cate2_arr[0])
				{
					$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate2_arr[0]}");
					if($query->num_rows() > 0) $cate2 = $query->row();

					if($cate2->MiddleCode)
					{
						$goods_cate .= '>'.$cate2->MiddleCode;

						if($goods_cate3_arr[0])
						{
							$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate3_arr[0]}");
							if($query->num_rows() > 0) $cate3 = $query->row();

							if($cate3->SmallCode)
								$goods_cate .= '>'.$cate3->SmallCode;
						}
					}
				}
			}
		}
		//debug($goods_cate);

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		/*
		foreach($excel_data AS $k => $v)
		{
			$headers[] = $v[0];
		}
		*/
		//debug($headers);

		// 엑셀 데이타
		$excel_data['A'][1] = $goods_info->GoodsName;	// 상품명[필수]

		$excel_data['F'][1] = $goods_info->GoodsCode;	// 자체상품코드

        // 브랜드코드 반영(2021.07.01)
        if($goods_info->GoodsCode_6)
        {
		    $excel_data['A'][1] = str_replace(strtoupper($goods_info->GoodsCode), strtoupper($goods_info->GoodsCode.$goods_info->GoodsCode_6), $goods_info->GoodsName);	// 상품명[필수]

		    $excel_data['F'][1] = $goods_info->GoodsCode.strtoupper($goods_info->GoodsCode_6);	// 자체상품코드
        }

		$excel_data['B'][1] = $goods_info->GoodsEtc7;	// 약어
		$excel_data['C'][1] = $goods_info->GoodsEtc5;	// 모델명

		// 브랜드명 추가 2020.05.27
		if($goods_info->BrandName)
			$excel_data['E'][1] = $goods_info->BrandName;	// 브랜드명

		$excel_data['G'][1] = str_replace("#", ",", substr_replace($goods_info->GoodsEtc24, "", 0, 1));	// 사이트검색어
		
		//2025.9.18. 오한아님 요청으로 H열 삽입
		$excel_data['H'][1] = "";
		
		$excel_data['I'][1] = $goods_info->GoodsEtc48;	// 상품구분[필수]
		$excel_data['J'][1] = $goods_cate;				// 상품분류 (대>중>소>세)

		// 2019.04.09 추가
		if($goods_info->sabangnet_id) $excel_data['K'][1] = $goods_info->sabangnet_id;	// 매입처ID

		if($goods_info->MakerName) $excel_data['M'][1] = $goods_info->MakerName;	// 제조사[필수]

		$excel_data['N'][1] = $goods_info->GoodsEtc21;	// 원산지(제조국)[필수]
		$excel_data['P'][1] = str_replace('-', '', substr($goods_info->created, 0, 10));	// 제조일(상품등록일로 변경 - 2021.10.15)
		$excel_data['Q'][1] = $goods_info->GoodsEtc51;	// 시즌
		$excel_data['S'][1] = $goods_info->GoodsEtc52;	// 상품상태[필수]
		$excel_data['U'][1] = $goods_info->GoodsEtc53;	// 세금구분[필수]
		$excel_data['V'][1] = $goods_info->GoodsEtc54;	// 배송비구분[필수]

		if($goods_info->GoodsEtc55 > 0)
			$excel_data['W'][1] = $goods_info->GoodsEtc55;	// 배송비

		$excel_data['Y'][1] = $goods_info->GoodsEtc9;	// 원가
		$excel_data['Z'][1] = $goods_info->GoodsPrice;	// 판매가[필수]
		$excel_data['AA'][1] = $goods_info->GoodsEtc32;	// TAG가[필수]

		$excel_data['AC'][1] = $goods_info->OptionColor;	// 옵션상세명칭(1)
		$excel_data['AE'][1] = $goods_info->OptionSize;	// 옵션상세명칭(2)

		$excel_data['AZ'][1] = $goods_info->GoodsEtc56;	// 재고관리사용여부
		$excel_data['BC'][1] = $goods_info->GoodsEtc10;	// 원가2

		// 상품코드별 이미지경로 자동생성
		if(!$goods_info->GoodsEtc60) $goods_info->GoodsEtc60 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_1.jpg';	// 대표이미지
		if(!$goods_info->GoodsEtc61) $goods_info->GoodsEtc61 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_1.jpg';	// 종합몰jpg이미지
		if(!$goods_info->GoodsEtc62) $goods_info->GoodsEtc62 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_2.jpg';	// 부가이미지2
		if(!$goods_info->GoodsEtc63) $goods_info->GoodsEtc63 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_1.jpg';	// 부가이미지4
		if(!$goods_info->GoodsEtc64) $goods_info->GoodsEtc64 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_2.jpg';	// 부가이미지5
		if(!$goods_info->GoodsEtc65) $goods_info->GoodsEtc65 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_3.jpg';	// 부가이미지6
		if(!$goods_info->GoodsEtc66) $goods_info->GoodsEtc66 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_4.jpg';	// 부가이미지7
		if(!$goods_info->GoodsEtc67) $goods_info->GoodsEtc67 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_5.jpg';	// 부가이미지8
		if(!$goods_info->GoodsEtc68) $goods_info->GoodsEtc68 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_6.jpg';	// 부가이미지9
		if(!$goods_info->GoodsEtc69) $goods_info->GoodsEtc69 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-270.gif';	// 부가이미지11
		if(!$goods_info->GoodsEtc70) $goods_info->GoodsEtc70 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-270.gif';	// 부가이미지12
		if(!$goods_info->GoodsEtc74) $goods_info->GoodsEtc74 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-list3.jpg';	// 부가이미지15(2019.10.23 추가)
		if(!$goods_info->GoodsEtc71) $goods_info->GoodsEtc71 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_1.jpg';	// 부가이미지20
		if(!$goods_info->GoodsEtc72) $goods_info->GoodsEtc72 = 'http://danharoo.negagea.kr/mimi/'.strtolower($goods_info->GoodsCode).'-600_3.jpg';	// 부가이미지22

		$excel_data['AF'][1] = $goods_info->GoodsEtc60;	// 대표이미지[필수]
		$excel_data['AG'][1] = $goods_info->GoodsEtc61;	// 종합몰JPG이미지
		$excel_data['AH'][1] = $goods_info->GoodsEtc62;	// 부가이미지2
		$excel_data['AJ'][1] = $goods_info->GoodsEtc63;	// 부가이미지4
		$excel_data['AK'][1] = $goods_info->GoodsEtc64;	// 부가이미지5
		$excel_data['AL'][1] = $goods_info->GoodsEtc65;	// 부가이미지6
		$excel_data['AM'][1] = $goods_info->GoodsEtc66;	// 부가이미지7
		$excel_data['AN'][1] = $goods_info->GoodsEtc67;	// 부가이미지8
		$excel_data['AO'][1] = $goods_info->GoodsEtc68;	// 부가이미지9
		$excel_data['BD'][1] = $goods_info->GoodsEtc69;	// 부가이미지11
		// $excel_data['BE'][1] = $goods_info->GoodsEtc70;	// 부가이미지12(2021.08.26 빈값으로 주석처리)
		$excel_data['BI'][1] = $goods_info->GoodsEtc74;	// 부가이미지15(2019.10.23 추가)
		$excel_data['BN'][1] = $goods_info->GoodsEtc71;	// 부가이미지20
		$excel_data['BP'][1] = $goods_info->GoodsEtc72;	// 부가이미지22

		$excel_data['BQ'][1] = 'http://shop.newtalk.kr/goods/detail/'.$goods_info->id;	// 관리자메모

		$excel_data['BS'][1] = $goods_info->GoodsEtc4;	// 영문상품명
		$excel_data['BT'][1] = $goods_info->GoodsEtc8;	// 출력상품명
		$excel_data['BV'][1] = $goods_info->GoodsEtc27;	// 추가 상품상세설명_1(선택입력)
		$excel_data['BW'][1] = $goods_info->GoodsEtc28;	// 추가 상품상세설명_2(선택입력)
		$excel_data['BX'][1] = $goods_info->GoodsEtc29;	// 추가 상품상세설명_3(선택입력)

		// 2020.05.13 속성분류코들별 사방넷 속상값 필드 변경
		$excel_data['CB'][1] = $goods_info->GoodsEtc57;	// 속성분류코드[필수]

        // 제조사(수입자/병행수입) 필드값 반영(2021.10.15)
		$excel_data['CF'][1] = $goods_info->MakerName ? $goods_info->MakerName : '협력업체/단하루 OEM';	// 속성값4[필수/선택]

        // 제조국이 대한민국일 경우 'N'값 타국일 경우 'Y'값 반영(2021.10.15)
		$excel_data['CM'][1] = (trim($goods_info->GoodsEtc21) === '대한민국') ? 'N' : 'Y';	// 속성값11[필수/선택]
		
		//2025.9.18. 오한아님 요청으로 속성분류가 001인 경우 속성값7의 데이터 값을 등록년월[ 202509 ]로 변경
		if($goods_info->GoodsEtc57 == '001')
		{
			$excel_data['CI'][1] = date("Ym", strtotime($goods_info->created));	// 속성값7[필수/선택]
		}

		// 구두/신발
		if($goods_info->GoodsEtc57 == '002')
		{
			$excel_data['CC'][1] = '상세페이지참조';	// 속성값1[필수/선택]
			$excel_data['CD'][1] = '상세페이지참조';	// 속성값2[필수/선택]
			$excel_data['CE'][1] = '상세페이지참조';	// 속성값3[필수/선택]
		    $excel_data['CF'][1] = $goods_info->MakerName ? $goods_info->MakerName : '협력업체/단하루 OEM';	// 속성값4[필수/선택]
			$excel_data['CG'][1] = '상세페이지참조';	// 속성값5[필수/선택]
			$excel_data['CH'][1] = '상세페이지참조';	// 속성값6[필수/선택]
			$excel_data['CI'][1] = '상세페이지참조';	// 속성값7[필수/선택]
			$excel_data['CJ'][1] = '고객센터 070-4333-0420';	// 속성값8[필수/선택]
			$excel_data['CK'][1] = '상세페이지참조';	// 속성값9[필수/선택]
			$excel_data['CL'][1] = '상세페이지참조';	// 속성값10[필수/선택]
		    $excel_data['CM'][1] = (trim($goods_info->GoodsEtc21) === '대한민국') ? 'N' : 'Y';	// 속성값11[필수/선택]
			$excel_data['CN'][1] = '상세페이지참조';	// 속성값12[필수/선택]
			$excel_data['CO'][1] = '상세페이지참조';	// 속성값13[필수/선택]
			$excel_data['CP'][1] = '상세페이지참조';	// 속성값14[필수/선택]
			$excel_data['CQ'][1] = '상세페이지참조';	// 속성값15[필수/선택]
			$excel_data['CR'][1] = '상세페이지참조';	// 속성값16[필수/선택]
		}
		// 가방
		if($goods_info->GoodsEtc57 == '003')
		{
			$excel_data['CC'][1] = '상세페이지참조';	// 속성값1[필수/선택]
			$excel_data['CD'][1] = '상세페이지참조';	// 속성값2[필수/선택]
			$excel_data['CE'][1] = '상세페이지참조';	// 속성값3[필수/선택]
		    $excel_data['CF'][1] = $goods_info->MakerName ? $goods_info->MakerName : '협력업체/단하루 OEM';	// 속성값4[필수/선택]
			$excel_data['CG'][1] = '상세페이지참조';	// 속성값5[필수/선택]
			$excel_data['CH'][1] = '상세페이지참조';	// 속성값6[필수/선택]
			$excel_data['CI'][1] = '고객센터 070-4333-0420';	// 속성값7[필수/선택]
			$excel_data['CJ'][1] = '상세페이지참조';	// 속성값8[필수/선택]
			$excel_data['CK'][1] = '상세페이지참조';	// 속성값9[필수/선택]
			$excel_data['CL'][1] = '상세페이지참조';	// 속성값10[필수/선택]
		    $excel_data['CM'][1] = (trim($goods_info->GoodsEtc21) === '대한민국') ? 'N' : 'Y';	// 속성값11[필수/선택]
			$excel_data['CN'][1] = '상세페이지참조';	// 속성값12[필수/선택]
			$excel_data['CO'][1] = '상세페이지참조';	// 속성값13[필수/선택]
			$excel_data['CP'][1] = '';	// 속성값14[필수/선택]
			$excel_data['CQ'][1] = '';	// 속성값15[필수/선택]
			$excel_data['CR'][1] = '';	// 속성값16[필수/선택]
		}
		// 패션잡화
		if($goods_info->GoodsEtc57 == '004')
		{
			$excel_data['CC'][1] = '상세페이지참조';	// 속성값1[필수/선택]
			$excel_data['CD'][1] = '상세페이지참조';	// 속성값2[필수/선택]
			$excel_data['CE'][1] = '상세페이지참조';	// 속성값3[필수/선택]
		    $excel_data['CF'][1] = $goods_info->MakerName ? $goods_info->MakerName : '협력업체/단하루 OEM';	// 속성값4[필수/선택]
			$excel_data['CG'][1] = '상세페이지참조';	// 속성값5[필수/선택]
			$excel_data['CH'][1] = '상세페이지참조';	// 속성값6[필수/선택]
			$excel_data['CI'][1] = '상세페이지참조';	// 속성값7[필수/선택]
			$excel_data['CJ'][1] = '고객센터 070-4333-0420';	// 속성값8[필수/선택]
			$excel_data['CK'][1] = '상세페이지참조';	// 속성값9[필수/선택]
			$excel_data['CL'][1] = '상세페이지참조';	// 속성값10[필수/선택]
		    $excel_data['CM'][1] = (trim($goods_info->GoodsEtc21) === '대한민국') ? 'N' : 'Y';	// 속성값11[필수/선택]
			$excel_data['CN'][1] = '상세페이지참조';	// 속성값12[필수/선택]
			$excel_data['CO'][1] = '상세페이지참조';	// 속성값13[필수/선택]
			$excel_data['CP'][1] = '상세페이지참조';	// 속성값14[필수/선택]
			$excel_data['CQ'][1] = '';	// 속성값15[필수/선택]
			$excel_data['CR'][1] = '';	// 속성값16[필수/선택]
		}

		// 2018.10.10 추가필드
		$excel_data['DD'][1] = $goods_info->GoodsEtc10;	// 원가2
		$excel_data['DE'][1] = $goods_info->GoodsEtc35;	// 미미야/스토어팜 판매가
		$excel_data['DF'][1] = $goods_info->GoodsEtc33;	// 단하루판매가
		$excel_data['DG'][1] = $goods_info->GoodsEtc34;	// 도매꾹/오너클랜/도매창고판매가
		$excel_data['DH'][1] = $goods_info->GoodsEtc41;	// 뉴톡판매가

		// 2018.11.08 추가필드
		$excel_data['DI'][1] = $goods_info->created;	// 뉴톡 등록일자
		$excel_data['DJ'][1] = $goods_info->modified;	// 뉴톡 수정일자

		$goods_info_html_data = $this->config->item('goods_info_html_data');
        // 이미지경로 뉴톡으로 반영
        // if($goods_info->modified >= $this->info['modified'])
        //     $goods_info_html_data = str_replace("{GoodsImgPath}", "http://newtalk.kr/data/files/goods/img/".$goods_info->GoodsCode."/", $goods_info_html_data);
        // else
        //     $goods_info_html_data = str_replace("{GoodsImgPath}", "http://danharoo.negagea.kr/mimi/", $goods_info_html_data);
        // 이미지경로 외부도메인 반영(2018.02.20)
        if($goods_info->GoodsEtc73)
            $goods_info_html_data = str_replace("{GoodsImgPath}", $goods_info->GoodsEtc73, $goods_info_html_data);
        else
            $goods_info_html_data = str_replace("{GoodsImgPath}", "http://newtalk.kr/data/files/goods/img/".$goods_info->GoodsCode."/", $goods_info_html_data);

		$goods_info_html_data = str_replace("{GoodsCode}", strtolower($goods_info->GoodsCode), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsName}", $excel_data['A'][1], $goods_info_html_data);
		$goods_info_html_data = str_replace("{Description}", nl2br($goods_info->Description), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc24}", '['.$goods_info->GoodsEtc24.']', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc25}", $goods_info->GoodsEtc25, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc26}", $goods_info->GoodsEtc26, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc16}", $goods_info->GoodsEtc16, $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionColor}", $goods_info->OptionColor, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc15}", $goods_info->GoodsEtc15, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc13}", ',사이즈('.$goods_info->GoodsEtc13.')', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc18}", $goods_info->GoodsEtc18, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc17}", $goods_info->GoodsEtc17, $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionSize}", $goods_info->OptionSize, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc14}", nl2br($goods_info->GoodsEtc14), $goods_info_html_data);
		$goods_info_html_data = str_replace("{MakerName}", $goods_info->MakerName, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc20}", $goods_info->GoodsEtc20, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc21}", $goods_info->GoodsEtc21, $goods_info_html_data);
		//$goods_info_html_data = str_replace("{GoodsEtc22}", nl2br($goods_info->GoodsEtc22), $goods_info_html_data);
		// ============================================================
		// 사방넷 엑셀 상품명 치환 (2026-02-12 3단계 분기 최종)
		// ============================================================
		// 1) aq_product_code 생성
		$aq_product_code = $goods_info->GoodsCode;
		if (!empty($goods_info->GoodsCode_6)) {
			$aq_product_code = $goods_info->GoodsCode . strtoupper($goods_info->GoodsCode_6);
		}
		// 2) replace_name 결정 — 3단계 분기
		if (stripos($goods_info->GoodsName, $aq_product_code) === 0) {
			// 단계1: GoodsName이 이미 전체코드(GoodsCode+GoodsCode_6)로 시작
			$replace_name = $goods_info->GoodsName;
		} elseif (!empty($goods_info->GoodsCode_6) && stripos($goods_info->GoodsName, $goods_info->GoodsCode) === 0) {
			// 단계2: GoodsName이 GoodsCode로만 시작 (GoodsCode_6 미포함)
			$replace_name = str_replace(
				strtoupper($goods_info->GoodsCode),
				strtoupper($goods_info->GoodsCode . $goods_info->GoodsCode_6),
				$goods_info->GoodsName
			);
		} else {
			// 단계3: 그 외 (코드 없음/기타)
			$replace_name = $aq_product_code . '-' . $goods_info->GoodsName;
		}
		// 3) DanharooDescription 내 [ 상품명 ] 치환 — 정규식으로 다양한 HTML 패턴 지원
		$description_html = $goods_info->DanharooDescription;
		if (!empty($description_html)) {
			$pattern = '/(<b[^>]*>(?:<font[^>]*>)?)\s*\[\s*(.*?)\s*\]\s*((?:<\/font>)?<\/b>)/';
			$replaced = preg_replace_callback($pattern, function($m) use ($replace_name) {
				return $m[1] . '[ ' . $replace_name . ' ]' . $m[3];
			}, $description_html, 1);
			if ($replaced !== null) {
				$description_html = $replaced;
			}
		}
		// 4) AQ열 할당
		$excel_data['AQ'][1] = $description_html;
		foreach($excel_data AS $k => $v)
		{
			$real_excel_data[] = $v[1];
		}
		//debug(count($real_excel_data));
		//debug($real_excel_data);exit;

		/*
		$rows = array(
			$real_excel_data,
		);
		//debug($rows);
		$data = array_merge(array($headers), $rows);
		//debug($data);

		// 스타일 지정
		$widths = array();
		foreach($excel_width_data AS $k => $v)
		{
			$widths[] = $v;
		}
		//debug(count($widths));
		$header_bgcolor = 'FFABCDEF';

		// 엑셀 생성
		$last_char = column_char( count($headers) - 1 );
		//debug($last_char);

		$excel = new PHPExcel();
		//$excel->setActiveSheetIndex(0)->getStyle( "A1:${last_char}1" )->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB($header_bgcolor);
		//$excel->setActiveSheetIndex(0)->getStyle( "A:$last_char" )->getAlignment()->setVertical(PHPExcel_Style_Alignment::VERTICAL_CENTER)->setWrapText(true);
		//foreach($widths as $i => $w) $excel->setActiveSheetIndex(0)->getColumnDimension( column_char($i) )->setWidth($w);
		//$excel->getActiveSheet()->fromArray($data, NULL, 'A1');
		//$writer = PHPExcel_IOFactory::createWriter($excel, 'Excel2007');
		//$writer->save($this->config->item('user_temp_image_dir').$goods_code.'.xlsx');

		$col1 = 0;
		foreach ($headers as $title)
		{
			$excel->setActiveSheetIndex(0)->setCellValueByColumnAndRow($col1, 1, $title);
			$col1++;
		}
		$col2 = 0;
		foreach($real_excel_data as $record)
		{
			$excel->setActiveSheetIndex(0)->setCellValueByColumnAndRow($col2, 2, $record);
			$col2++;
		}

		$excel->getActiveSheet()->setTitle('단하루 상품');
		$excel->setActiveSheetIndex(0);
		$writer = PHPExcel_IOFactory::createWriter($excel, 'Excel5');

		$rtn_file_path = $this->config->item('user_temp_image_dir').$this->view_data['user_id'].'/'.$goods_info->GoodsCode.'.xls';

		$writer->save($rtn_file_path);
		*/

		return $real_excel_data;
	}

    // 상품 엑셀 셀메이트 데이타 처리
	function goods_excel_sellmate_data($goods_info, $option, $option_china)
	{
		// debug($goods_info);
		$this->load->model('goods_m');
		$etc = $this->goods_m->get_goods_option_etc('status');
		$excel_data = $this->config->item('sellmate_excel_data');
		//debug($excel_width_data);

		$rtn_file_path = '';

		// 상품분류 (대>중>소>세)
		$goods_cate = '';
		$goods_cate1 = $goods_info->Category1;
		$goods_cate2_arr = explode('|', $goods_info->Category2);
		$goods_cate3_arr = explode('|', $goods_info->Category3);
		if($goods_cate1)
		{
			$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate1}");
			if($query->num_rows() > 0) $cate1 = $query->row();

			if($cate1->LargeCode)
			{
				$goods_cate .= $cate1->LargeCategory;

				if($goods_cate2_arr[0])
				{
					$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate2_arr[0]}");
					if($query->num_rows() > 0) $cate2 = $query->row();

					if($cate2->MiddleCode)
					{
						$goods_cate .= '>'.$cate2->MiddleCategory;

						if($goods_cate3_arr[0])
						{
							$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate3_arr[0]}");
							if($query->num_rows() > 0) $cate3 = $query->row();

							if($cate3->SmallCode)
								$goods_cate .= '>'.$cate3->SmallCategory;
						}
					}
				}
			}
		}
		//debug($goods_cate);

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		/*
		foreach($excel_data AS $k => $v)
		{
			$headers[] = $v[0];
		}
		*/
		//debug($headers);

		// 엑셀 데이타
        $excel_data['A'][1] = $goods_cate;	// 카테고리(대분류>중분류>소분류)
		$excel_data['B'][1] = $goods_info->GoodsEtc6;	// 매입처
		$excel_data['F'][1] = $goods_info->GoodsEtc5;	// 모델명(사입명)

		// 2020.01.02 상품명에 상품코드 제거
		// $excel_data['G'][1] = str_replace(strtoupper($goods_info->GoodsCode).'-', '', strtoupper($goods_info->GoodsName));	// 상품명
		// 2020.12.14 상품명(자사몰) 반영
		$excel_data['G'][1] = $goods_info->GoodsName;	// 상품명

		// 옵션 조합형 처리
		// $OptionColorArr = explode(',', $goods_info->OptionColor);
		// $OptionSizeArr = explode(',', $goods_info->OptionSize);
		// $OptionValueArr = [];
		// for($i=0; $i<count($OptionColorArr); $i++)
		// {
		// 	for($j=0; $j<count($OptionSizeArr); $j++)
		// 	{
		// 		$OptionValueArr[] = $OptionColorArr[$i].':'.$OptionSizeArr[$j];
		// 	}
		// }
		// debug(implode('\n', $OptionValueArr));
		$excel_data['H'][1] = $option_china;	// 중국옵션명
		$excel_data['I'][1] = $option;			// 옵션명
		// $excel_data['I'][1] = implode(chr(10), $OptionValueArr);	// 옵션명

		$excel_data['J'][1] = $goods_info->GoodsEtc9;	// 원가
		$excel_data['M'][1] = $goods_info->GoodsPrice;	// 판매가
		$excel_data['P'][1] = $goods_info->GoodsEtc32;	// TAG가
		$excel_data['W'][1] = $goods_info->GoodsEtc21;	// 원산지
		$excel_data['AB'][1] = $goods_info->GoodsEtc10;	// 원가2(중국원화원가)
		$excel_data['AC'][1] = $goods_info->GoodsEtc8;	// 출력상품명(중국상품 품번)
		$excel_data['AD'][1] = $goods_info->GoodsEtc7;	// 상품약어
		$excel_data['AE'][1] = $etc['status'][$goods_info->GoodsEtc52];	// 상품상태
		$excel_data['AF'][1] = $goods_info->created;	// 등록일시
		$excel_data['AF'][1] = $goods_info->GoodsEtc7;	// 상품약어
		$excel_data['AG'][1] = $goods_info->GoodsCode;	// 상품코드
		$excel_data['BC'][1] = $goods_info->GoodsEtc60;	// 대표이미지

		foreach($excel_data AS $k => $v)
		{
			$real_excel_data[] = $v[1];
		}
		//debug(count($real_excel_data));
		//debug($real_excel_data);exit;

		return $real_excel_data;
	}

    // 상품 엑셀 디디지 데이타 처리
	function goods_excel_ddg_data($goods_info)
	{
		//debug($goods_info);
		$excel_data = $this->config->item('ddg_excel_data');
		//debug($excel_width_data);

		$rtn_file_path = '';

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		/*
		foreach($excel_data AS $k => $v)
		{
			$headers[] = $v[0];
		}
		*/
		//debug($headers);

		// 엑셀 데이타
		$excel_data['A'][1] = strtolower($goods_info->GoodsCode);	// 상품코드

		// $excel_data['E'][1] = $goods_info->GoodsName;	// 상품명[필수]
		// 2020.02.13 상품명에 상품코드 제거
		// $excel_data['E'][1] = str_replace(strtoupper($goods_info->GoodsCode).'-', '', strtoupper($goods_info->GoodsName));	// 상품명
		// 2020.12.14 상품명(자사몰)
		$excel_data['E'][1] = $goods_info->GoodsName;	// 상품명

		// 2020.02.13 제조국 원산지: 입력된 값으로 표기
		$excel_data['G'][1] = $goods_info->GoodsEtc21;	// 모델

		$excel_data['I'][1] = $goods_info->GoodsEtc5;	// 모델
        $excel_data['O'][1] = str_replace("#", ",", substr_replace($goods_info->GoodsEtc24, "", 0, 1));	// 기본설명

		$goods_info_html_data = $this->config->item('goods_info_html_data');
        // 이미지경로 뉴톡으로 반영
		if($goods_info->modified >= $this->info['modified'])
		{
			// 이미지경로 외부도메인 반영(2020.02.13)
			if($goods_info->GoodsEtc73)
				$goods_info_html_data = str_replace("{GoodsImgPath}", $goods_info->GoodsEtc73, $goods_info_html_data);
			else
            	$goods_info_html_data = str_replace("{GoodsImgPath}", "http://newtalk.kr/data/files/goods/img/".$goods_info->GoodsCode."/", $goods_info_html_data);
		}
        else
            $goods_info_html_data = str_replace("{GoodsImgPath}", "http://danharoo.negagea.kr/mimi/", $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsCode}", strtolower($goods_info->GoodsCode), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsName}", $excel_data['C'][1], $goods_info_html_data);
		$goods_info_html_data = str_replace("{Description}", nl2br($goods_info->Description), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc24}", '['.$goods_info->GoodsEtc24.']', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc25}", $goods_info->GoodsEtc25, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc26}", nl2br($goods_info->GoodsEtc26), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc16}", $goods_info->GoodsEtc16, $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionColor}", $goods_info->OptionColor, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc15}", nl2br($goods_info->GoodsEtc15), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc13}", ',사이즈('.$goods_info->GoodsEtc13.')', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc18}", $goods_info->GoodsEtc18, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc17}", $goods_info->GoodsEtc17, $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionSize}", $goods_info->OptionSize, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc14}", nl2br($goods_info->GoodsEtc14), $goods_info_html_data);
		$goods_info_html_data = str_replace("{MakerName}", $goods_info->MakerName, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc20}", $goods_info->GoodsEtc20, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc21}", $goods_info->GoodsEtc21, $goods_info_html_data);
		//$goods_info_html_data = str_replace("{GoodsEtc22}", nl2br($goods_info->GoodsEtc22), $goods_info_html_data);
		// $excel_data['P'][1] = $goods_info_html_data;	// 상품설명[필수]
        // $excel_data['Q'][1] = $goods_info_html_data;	// 모바일상품설명
		// $excel_data['P'][1] = $goods_info->DanharooDescription;	// 상품설명[필수]
		// $excel_data['Q'][1] = $goods_info->DanharooDescription;	// 모바일상품설명
		// 변경(2020.12.15)
		$excel_data['P'][1] = $excel_data['Q'][1] = str_replace('<b><font size="3.5">[ '.$goods_info->DanharooGoodsName.' ]</font></b>', '<b><font size="3.5">[ '.$excel_data['E'][1].' ]</font></b>', $goods_info->DanharooDescription);

		$excel_data['R'][1] = $goods_info->GoodsPrice;	// 시중가격(판매가)

		// $excel_data['S'][1] = $goods_info->GoodsEtc33;	// 판매가격(단하루판매가)
		// 2020.02.13 판매가격 미미야 판매가격으로 변경
		$excel_data['S'][1] = $goods_info->GoodsEtc35;	// 판매가격(단하루판매가)

		$excel_data['AF'][1] = $goods_info->OptionColor.'|'.$goods_info->OptionSize;	// 옵션항목

        // 이미지경로 자동생성
		if($goods_info->GoodsEtc60) $excel_data['AG'][1] = $goods_info->GoodsEtc60;	// 이미지1
		if($goods_info->GoodsEtc62) $excel_data['AH'][1] = $goods_info->GoodsEtc62;	// 이미지2
		if($goods_info->GoodsEtc63) $excel_data['AI'][1] = $goods_info->GoodsEtc63;	// 이미지3
		if($goods_info->GoodsEtc64) $excel_data['AJ'][1] = $goods_info->GoodsEtc64;	// 이미지4
		if($goods_info->GoodsEtc65) $excel_data['AK'][1] = $goods_info->GoodsEtc65;	// 이미지5
		if($goods_info->GoodsEtc66) $excel_data['AL'][1] = $goods_info->GoodsEtc66;	// 이미지6
		if($goods_info->GoodsEtc67) $excel_data['AM'][1] = $goods_info->GoodsEtc67;	// 이미지7
		if($goods_info->GoodsEtc68) $excel_data['AN'][1] = $goods_info->GoodsEtc68;	// 이미지8
		if($goods_info->GoodsEtc69) $excel_data['AO'][1] = $goods_info->GoodsEtc69;	// 이미지9
		if($goods_info->GoodsEtc70) $excel_data['AP'][1] = $goods_info->GoodsEtc70;	// 이미지10

		foreach($excel_data AS $k => $v)
		{
			$real_excel_data[] = $v[1];
		}
		//debug(count($real_excel_data));
		//debug($real_excel_data);exit;

		return $real_excel_data;
	}

    // 상품 엑셀 뉴톡 데이타 처리
	function goods_excel_newtalk_data($goods_info)
	{
		//debug($goods_info);
		$excel_data = $this->config->item('newtalk_excel_data');
		//debug($excel_width_data);

		$rtn_file_path = '';

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		/*
		foreach($excel_data AS $k => $v)
		{
			$headers[] = $v[0];
		}
		*/
		//debug($headers);

		// 엑셀 데이타
        $excel_data['A'][1] = $goods_info->market;	// 상품마켓
        $excel_data['B'][1] = $goods_info->Category1;	// 대분류
        $excel_data['C'][1] = $goods_info->Category2;	// 중분류
        $excel_data['D'][1] = $goods_info->Category3;	// 소분류
		$excel_data['E'][1] = $goods_info->activated;	// 상품노출여부
        $excel_data['F'][1] = $goods_info->GoodsImage;	// 상품이미지
        $excel_data['G'][1] = strtolower($goods_info->GoodsCode);	// 상품코드
		$excel_data['H'][1] = $goods_info->GoodsName;	// 상품명
		$excel_data['I'][1] = $goods_info->GoodsEtc4;	// 영문상품명
		$excel_data['J'][1] = $goods_info->GoodsEtc7;	// 상품약어(중국사이트 링크)
		$excel_data['K'][1] = $goods_info->GoodsEtc8;	// 출력상품명(중국 상품 품번)
		$excel_data['L'][1] = $goods_info->GoodsEtc5;	// 모델명(사입명)
		$excel_data['M'][1] = $goods_info->GoodsEtc6;	// 매입처
		$excel_data['N'][1] = $goods_info->GoodsEtc9;	// 원가
		$excel_data['O'][1] = $goods_info->GoodsEtc10;	// 원가2(중국 원화원가)
		$excel_data['P'][1] = $goods_info->GoodsPrice;	// 판매가
		$excel_data['Q'][1] = $goods_info->GoodsEtc32;	// TAG가
		$excel_data['R'][1] = $goods_info->GoodsEtc35;	// 미미야/스토어팜 판매가
		$excel_data['S'][1] = $goods_info->GoodsEtc33;	// 단하루판매가
		$excel_data['T'][1] = $goods_info->GoodsEtc34;	// 도매꾹/오너클랜/도매창고 판매가
		$excel_data['U'][1] = $goods_info->GoodsEtc41;	// 뉴톡판매가
		$excel_data['V'][1] = $goods_info->GoodsEtc48;	// 상품구분
		$excel_data['W'][1] = $goods_info->GoodsEtc51;	// 시즌
		$excel_data['X'][1] = $goods_info->GoodsEtc52;	// 상품상태
		$excel_data['Y'][1] = $goods_info->Description;	// 상품상세설명
		$excel_data['Z'][1] = $goods_info->GoodsEtc24;	// 사이트 검색어

		$excel_data['AA'][1] = $goods_info->GoodsEtc25;	// 모델사이즈
		$excel_data['AB'][1] = $goods_info->GoodsEtc26;	// 모델 착용 사이즈
		$excel_data['AC'][1] = $goods_info->OptionColor;	// COLOR(색상)
		$excel_data['AD'][1] = $goods_info->OptionSize;	// SIZE(사이즈)
		$excel_data['AE'][1] = $goods_info->SocialGoodsOption;	// 쇼셜용 옵션
		$excel_data['AF'][1] = $goods_info->GoodsEtc16;	// FABRIC(소재)
		$excel_data['AG'][1] = $goods_info->GoodsEtc14;	// SizeSpec(상세사이즈)
		$excel_data['AH'][1] = $goods_info->GoodsEtc13;	// Fit(사이즈감)
		$excel_data['AI'][1] = $goods_info->GoodsEtc15;	// 원단느낌
		$excel_data['AJ'][1] = $goods_info->GoodsEtc18;	// WASHING(세탁방법)
		$excel_data['AK'][1] = $goods_info->GoodsEtc17;	// Weight(무게:g)
		$excel_data['AL'][1] = $goods_info->MakerName;	// 제조사
		$excel_data['AM'][1] = $goods_info->GoodsEtc20;	// 제조일
		$excel_data['AN'][1] = $goods_info->GoodsEtc21;	// 제조국(원산지)
		$excel_data['AO'][1] = $goods_info->GoodsEtc22;	// 품질보증기준
		$excel_data['AP'][1] = $goods_info->GoodsEtc27;	// 추가상품상세설명1
		$excel_data['AQ'][1] = $goods_info->GoodsEtc28;	// 추가상품상세설명2
		$excel_data['AR'][1] = $goods_info->GoodsEtc29;	// 추가상품상세설명3
		$excel_data['AS'][1] = $goods_info->GoodsEtc30;	// 기타 상세설명
		$excel_data['AT'][1] = $goods_info->GoodsEtc58;	// 신상마켓 상세설명
		$excel_data['AU'][1] = $goods_info->GoodsEtc59;	// 카카오스토리 상세설명
		$excel_data['AV'][1] = $goods_info->GoodsEtc36;	// 쇼핑몰별 상품명1
		$excel_data['AW'][1] = $goods_info->GoodsEtc37;	// 쇼핑몰별 상품명2
		$excel_data['AX'][1] = $goods_info->GoodsEtc38;	// 쇼핑몰별 상품명3
		$excel_data['AY'][1] = $goods_info->GoodsEtc39;	// 카페24 상품명
		$excel_data['AZ'][1] = $goods_info->GoodsEtc40;	// 뉴톡 상품명

		$excel_data['BA'][1] = $goods_info->SellingPeriod;	// 판매기간
		$excel_data['BB'][1] = $goods_info->GoodsCount;	// 판매수량
		$excel_data['BC'][1] = $goods_info->GoodsEtc56;	// 재고관리사용여부
		$excel_data['BD'][1] = $goods_info->GoodsEtc57;	// 속성분류코드
		$excel_data['BE'][1] = $goods_info->GoodsEtc53;	// 세금구분
		$excel_data['BF'][1] = $goods_info->GoodsEtc54;	// 배송비구분
		$excel_data['BG'][1] = $goods_info->GoodsEtc55;	// 배송비
		$excel_data['BH'][1] = $goods_info->GoodsEtc73;	// 외부이미지 도메인경로
		$excel_data['BI'][1] = $goods_info->GoodsEtc60;	// 대표이미지
		$excel_data['BJ'][1] = $goods_info->GoodsEtc61;	// 종합몰jpg이미지
		$excel_data['BK'][1] = $goods_info->GoodsEtc62;	// 부가이미지2
		$excel_data['BL'][1] = $goods_info->GoodsEtc63;	// 부가이미지4
		$excel_data['BM'][1] = $goods_info->GoodsEtc64;	// 부가이미지5
		$excel_data['BN'][1] = $goods_info->GoodsEtc65;	// 부가이미지6
		$excel_data['BO'][1] = $goods_info->GoodsEtc66;	// 부가이미지7
		$excel_data['BP'][1] = $goods_info->GoodsEtc67;	// 부가이미지8
		$excel_data['BQ'][1] = $goods_info->GoodsEtc68;	// 부가이미지9
		$excel_data['BR'][1] = $goods_info->GoodsEtc69;	// 부가이미지11
		$excel_data['BS'][1] = $goods_info->GoodsEtc70;	// 부가이미지12

		// 2022.11.02 추가
		$excel_data['BT'][1] = $goods_info->GoodsEtc74;	// 부가이미지15
		$excel_data['BU'][1] = $goods_info->GoodsEtc71;	// 부가이미지20
		$excel_data['BV'][1] = $goods_info->GoodsEtc72;	// 부가이미지22

		// 2019.09.23 추가
		$excel_data['BW'][1] = $goods_info->mall_activated;		// 미니몰노출여부
		$excel_data['BX'][1] = $goods_info->DanharooGoodsName;	// 단하루상품명

		// 2019.12.19 추가
		$excel_data['BY'][1] = $goods_info->OptionColorChina;	// 중국 COLOR(색상)
		$excel_data['BZ'][1] = $goods_info->OptionSizeChina;	// 중국 SIZE(사이즈)

		// 2020.05.13 추가
		$excel_data['CA'][1] = $goods_info->DanharooDescription;	// 단하루상품설명

		// 2020.12.15 변경
		// $excel_data['BZ'][1] = str_replace('<b><font size="3.5">[ '.$goods_info->DanharooGoodsName.' ]</font></b>', '<b><font size="3.5">[ '.$goods_info->GoodsName.' ]</font></b>', $goods_info->DanharooDescription);

		$excel_data['CB'][1] = $goods_info->goods_id;	// 상품수정고유번호

		// 2020.05.28 추가
		$excel_data['CC'][1] = $goods_info->BrandName;	// 브랜드명

		// 2020.06.01 추가
		$excel_data['CD'][1] = $goods_info->GoodsOnly;	// 단독상품

		foreach($excel_data AS $k => $v)
		{
			$real_excel_data[] = $v[1];
		}
		//debug(count($real_excel_data));
		//debug($real_excel_data);exit;

		return $real_excel_data;
	}

    // 상품 선택 셀메이트 엑셀 만들기 2019.12.13 반영
	function goods_select_sellmate_make()
	{
		$this->load->model('goods_m');
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->load->library('excel');

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		// 엑셀 자료 생성
		$real_excel_data = [];	// 사방넷
		$headers = [];			// 사방넷
		//debug(count($headers));

        // 셀메이트 엑셀 샘플파일(2019.12.13) 로드
		$objPHPExcel = PHPExcel_IOFactory::load('/home/danharoo/www/data/files/excel/sellmate_newtalk.xls');
		$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
		//debug($sheetDataArr);

		// 시트를 지정한다.
		$objPHPExcel->setActiveSheetIndex(0);
		$objPHPExcel->getActiveSheet()->setTitle('단하루 상품');

		$row = 2;
		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE (1) AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();
						// $goods_info->OptionColorTxt = '';
						//debug($goods_info);

						// 옵션 조합형 처리
						$OptionColorArr = explode(',', $goods_info->OptionColor);
						$OptionSizeArr = explode(',', $goods_info->OptionSize);
						$OptionColorChinaArr = explode(',', $goods_info->OptionColorChina);
						$OptionSizeChinaArr = explode(',', $goods_info->OptionSizeChina);
						$OptionValueArr = [];
						for($i=0; $i<count($OptionColorArr); $i++)
						{
							for($j=0; $j<count($OptionSizeArr); $j++)
							{
								$OptionTxt = '';
								$OptionChinaTxt = '';

								if($OptionColorArr[$i] && $OptionSizeArr[$j])
									$OptionTxt = $OptionColorArr[$i].':'.$OptionSizeArr[$j];
								if($OptionColorChinaArr[$i] && $OptionSizeChinaArr[$j])
									$OptionChinaTxt = $OptionColorChinaArr[$i].':'.$OptionSizeChinaArr[$j];

								$records = $this->goods_excel_sellmate_data($goods_info, $OptionTxt, $OptionChinaTxt);	// 셀메이트
								//debug($record);
								$col = 0;
								foreach ($records as $record)
								{
									$objPHPExcel->getActiveSheet()->setCellValueByColumnAndRow($col, $row, $record);
									$col++;
								}

								$row++;
							}
						}

						$this->goods_m->down_status($this->view_data['user_id'], $goods_info->goods_id, '', 'S');

						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$writer = PHPExcel_IOFactory::createWriter($objPHPExcel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/danharoo_goods_sellmate_data.xls';
		$writer->save($rtn_file_path);

		//$this->zip->read_file($rtn_file_path);
		//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

    // 상품 선택 셀메이트 엑셀 압축 다운
	function goods_select_sellmate_excel_zip_down()
	{
        ini_set('memory_limit','-1');

        $this->load->helper('download');	// 다운로드 헬퍼로드

        $data = file_get_contents($this->config->item('user_temp_image_dir').'/danharoo_goods_sellmate_data.xls'); // Read the file's contents
        $name = urlencode('셀메이트등록엑셀파일').'('.date('YmdHis', time()).').xls';

        force_download($name, $data);
	}

	function goods_select_ddg_send(){
        $this->load->model('goods_m');
        //https://newtalk.kr/products/goods_select_ddg_send/?goodId=56550
		if($_SERVER['REMOTE_ADDR'] == "220.116.84.234" || $_SERVER['REMOTE_ADDR'] == "59.9.33.3"){
			//$good_idx = $this->input->get('goodId');
			$good_idx = $this->input->post('goodId');
		} else {
			$good_idx = $this->input->post('goodId');
		}

        if(!$good_idx)
        {
            echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
            exit;
        }

        $row = 3;

//        // 회원 상품이 맞는지 확인
//        $sql1 = " SELECT id FROM goods_master WHERE (1) AND id='{$good_idx}' ";
//        $query1 = $this->db->query($sql1);
//        //debug($query1->num_rows());
//
//        if($query1->num_rows() > 0)
//        {
//
//            else
//            {
//                echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
//                exit;
//            }
//        }
//        else
//        {
//            echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
//            exit;
//        }
        // 등록 상품 정보 확인
        $sql2 = "SELECT
                        GS.*,
                        GSD.*,
                        GSI.*
                        FROM
                            goods AS		GS LEFT OUTER JOIN
                            goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
                            goods_image		As GSI ON GS.id = GSI.goods_id
                        WHERE GS.id='{$good_idx}'
            ";
        $query2 = $this->db->query($sql2);
        //debug($query2->num_rows());exit;
        $master_goods_cnt = $query2->num_rows();

		if($_SERVER['REMOTE_ADDR'] == "220.116.84.234" || $_SERVER['REMOTE_ADDR'] == "59.9.33.3"){
			echo "sql2 = ".$sql2."<br>";
			//echo "master_goods_cnt = ".$master_goods_cnt."<br>";
			//exit;
		}

        if($master_goods_cnt > 0)
        {
            $this->config->item('user_temp_image_dir', 1);
            $goods_info = $query2->row();

            $goods_code = $goods_info->GoodsCode;
            $goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code;

//            $records = $this->goods_excel_ddg_data($goods_info);	// 디디지

            if(is_file($goods_img_dir.'/'.$goods_code."-s_1.jpg")){
                $goods_info->mobile_img= "https://newtalk.kr/data/files/goods/goodscode/img/".$goods_code.'/'.$goods_code."-s_1.jpg";
            }else {
                $goods_info->mobile_img = "none";
            }

            // $records = $this->goods_excel_ddg_data($goods_info);	// 디디지
            $records = $goods_info;
			if($_SERVER['REMOTE_ADDR'] == "220.116.84.234" || $_SERVER['REMOTE_ADDR'] == "59.9.33.3"){
				$url = "https://freeddg.com/ddga/shop_admin/itemInputApi_test.php";
				//echo "url = ".$url."<br>";
				//exit;
			} else {
				$url = "https://freeddg.com/ddga/shop_admin/itemInputApi.php";
			}
			$post_field_string = http_build_query($records, '', '&');
            
			$ch = curl_init();                                                            // curl 초기화
            curl_setopt($ch, CURLOPT_URL, $url);                                 // url 지정하기
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);              // 요청결과를 문자열로 반환
            curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 10);               // connection timeout : 10초
            curl_setopt($ch, CURLOPT_SSL_VERIFYPEER, false);                 // 원격 서버의 인증서가 유효한지 검사 여부
            curl_setopt($ch, CURLOPT_POSTFIELDS, $post_field_string);      // POST DATA
            curl_setopt($ch, CURLOPT_POST, true);                               // POST 전송 여부
            $response = curl_exec($ch);
            curl_close ($ch);
//                $res_result = json_decode($response);
//                $res_result = json_decode($res_result);
//                $res_result['data'] = $records;
//                echo $response;
            $res_data = json_decode($response);

            if($res_data->code == '0000'){
                $up_que = "UPDATE goods SET ddg_send_yn = 'Y', ddg_lastsend = '".date('Y-m-d H:i:s', time())."' WHERE id = '{$good_idx}' ";
                $upRes = $this->db->query($up_que);
            }
            echo $response;
            exit;
        }
    }

    // 상품 선택 디디지 엑셀 만들기 2017.09.11 반영
	function goods_select_ddg_make()
	{
		$this->load->model('goods_m');
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->load->library('excel');

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		// 엑셀 자료 생성
		$real_excel_data = [];	// 사방넷
		$headers = [];			// 사방넷
		//debug(count($headers));

        // 디디지 엑셀 샘플파일(2017.09.11) 로드
		$objPHPExcel = PHPExcel_IOFactory::load('/home/danharoo/www/data/files/excel/danharoo_ddg.xls');
		$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
		//debug($sheetDataArr);

		// 시트를 지정한다.
		$objPHPExcel->setActiveSheetIndex(0);
		$objPHPExcel->getActiveSheet()->setTitle('단하루 상품');

		$row = 3;
		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE (1) AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();
						//debug($this->goods_excel_data($query2->row()));

						$records = $this->goods_excel_ddg_data($goods_info);	// 디디지
						//debug($record);
						$col = 0;
						foreach ($records as $record)
						{
							$objPHPExcel->getActiveSheet()->setCellValueByColumnAndRow($col, $row, $record);
							$col++;
						}

						$this->goods_m->down_status($this->view_data['user_id'], $goods_info->goods_id, '', 'D');

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$writer = PHPExcel_IOFactory::createWriter($objPHPExcel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/danharoo_goods_ddg_data.xls';
		$writer->save($rtn_file_path);

		//$this->zip->read_file($rtn_file_path);
		//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

    // 상품 선택 디디지 엑셀 압축 다운
	function goods_select_ddg_excel_zip_down()
	{
        ini_set('memory_limit','-1');

        $this->load->helper('download');	// 다운로드 헬퍼로드

        $data = file_get_contents($this->config->item('user_temp_image_dir').'/danharoo_goods_ddg_data.xls'); // Read the file's contents
        $name = urlencode('디디지등록엑셀파일').'('.date('YmdHis', time()).').xls';

        force_download($name, $data);
	}

    // 상품 선택 뉴톡 엑셀 만들기 2018.10.19 반영
	function goods_select_newtalk_make()
	{
		$this->load->model('goods_m');
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->load->library('excel');

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		// 엑셀 자료 생성
		$real_excel_data = [];	// 사방넷
		$headers = [];			// 사방넷
		//debug(count($headers));

        // 뉴톡 엑셀 샘플파일(2018.10.19) 로드
		$objPHPExcel = PHPExcel_IOFactory::load('/home/danharoo/www/data/files/excel/danharoo_newtalk.xls');
		$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
		//debug($sheetDataArr);

		// 시트를 지정한다.
		$objPHPExcel->setActiveSheetIndex(0);
		$objPHPExcel->getActiveSheet()->setTitle('단하루 상품');

		$row = 2;
		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE (1) AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);
				//debug($query1->num_rows());

				if($query1->num_rows() > 0)
				{
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.*,
								GSD.*,
								GSI.*
								FROM
									goods AS		GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
									goods_image		As GSI ON GS.id = GSI.goods_id
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					//debug($query2->num_rows());exit;
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0)
					{
						$goods_info = $query2->row();
						//debug($this->goods_excel_data($query2->row()));

						// 상품분류 (대>중>소>세)
						$goods_cate = '';
						$goods_cate1 = $goods_info->Category1;
						$goods_cate2_arr = explode('|', $goods_info->Category2);
						$goods_cate3_arr = explode('|', $goods_info->Category3);
						if($goods_cate1)
						{
							$query3 = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate1}");
							if($query3->num_rows() > 0) $cate1 = $query3->row();

							if($cate1->LargeCode)
							{
								$goods_info->Category1 = $cate1->LargeCategory;

								if($goods_cate2_arr[0])
								{
									$query4 = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate2_arr[0]}");
									if($query4->num_rows() > 0) $cate2 = $query4->row();

									if($cate2->MiddleCode)
									{
										$goods_info->Category2 = $cate2->MiddleCategory;

										if($goods_cate3_arr[0])
										{
											$query5 = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate3_arr[0]}");
											if($query5->num_rows() > 0) $cate3 = $query5->row();

											if($cate3->SmallCode)
												$goods_info->Category3 = $cate3->SmallCategory;
										}
									}
								}
							}
						}

						$records = $this->goods_excel_newtalk_data($goods_info);	// 뉴톡
						//debug($record);
						$col = 0;
						foreach ($records as $record)
						{
							$objPHPExcel->getActiveSheet()->setCellValueByColumnAndRow($col, $row, $record);
							$col++;
						}

						$this->goods_m->down_status($this->view_data['user_id'], $goods_info->goods_id, '', 'D');

						$row++;
						$success_cnt++;
					}
					else
					{
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				}
				else
				{
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$writer = PHPExcel_IOFactory::createWriter($objPHPExcel, 'Excel5');
		$rtn_file_path = $this->config->item('user_temp_image_dir').'/danharoo_goods_newtalk_data.xls';
		$writer->save($rtn_file_path);

		//$this->zip->read_file($rtn_file_path);
		//$this->zip->download('danharoo_goods_'.date('YmdHis', time()).'.zip');

		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

    // 상품 선택 뉴톡 엑셀 압축 다운
	function goods_select_newtalk_excel_zip_down()
	{
        ini_set('memory_limit','-1');

        $this->load->helper('download');	// 다운로드 헬퍼로드

        $data = file_get_contents($this->config->item('user_temp_image_dir').'/danharoo_goods_newtalk_data.xls'); // Read the file's contents
        $name = urlencode('뉴톡수정엑셀파일').'('.date('YmdHis', time()).').xls';

        force_download($name, $data);
	}

	// 상품코드 이미지 관리
	function goods_img()
	{
		// debug($this->view_data['auth_code']);
		if($this->view_data['auth_code'] != 15 && $this->view_data['auth_code'] != 12)
			alert('권한이 없습니다!');

		if(!$this->uri->segment(3))
			alert_only('주소창 끝에 상품코드를 입력하세요!');

		$this->config->item('user_temp_image_dir', 1);

		$goods_code = $this->uri->segment(3);
		
		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code;
		$query = $this->db->query("SELECT id FROM goods_code WHERE gcode='{$goods_code}'");
		
		if($query->num_rows() == 0)
		{
			$insert_data = array(
						   'gcode' => $goods_code,
						   'created' => date('Y-m-d H:i:s', time())
						);

			$this->db->insert('goods_code', $insert_data);
		}

		if(!is_dir($goods_img_dir))
		{
			//debug($goods_img_dir);
			mkdir($goods_img_dir, 0777, TRUE);
		}

		// 상품코드별 필수파일 유무 체크
		$goods_code_img_checked = [
			'-s_1.jpg' => '무',
			'-newtalk.jpg' => '무',
			'-600_1.jpg' => '무',
			'-600_2.jpg' => '무',
			// '-600_3.jpg' => '무',
			'-g_1.gif' => '무',
			'_list1.jpg' => '무'
		];
		foreach ($goods_code_img_checked as $key => $value)
		{
			if(is_file($goods_img_dir.'/'.$goods_code.$key)) $goods_code_img_checked[$key] = '유';
		}
		// debug($goods_code_img_checked);

		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/assets/css/jquery.fileupload.css');
		$this->view_data['link_tag3'] = link_tag('/assets/css/jquery.fileupload-ui.css');
		$this->view_data['link_tag4'] = link_tag('/assets/css/ladda-themeless.min.css');
		$this->view_data['link_tag5'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$this->view_data['goods_code'] = $goods_code;
		$this->view_data['goods_code_img_checked'] = $goods_code_img_checked;
		
		// 오션 전송 마지막 로그 추출
		$this->load->model('goods_m');
        $this->view_data['ocean_last_date'] = $this->goods_m->get_goods_ocean_last_date($goods_code);
		$this->view_data['goods_info'] = $this->goods_m->get_goods_info($goods_code);

		$this->load->view('top', $this->view_data);
//		if($_SERVER["REMOTE_ADDR"] == "121.134.129.200"){
//			$this->load->view('products/goods_img_ocean_test', $this->view_data);
//		}else{
//	echo "<xmp>";
//	print_r($this->view_data);
//	echo "</xmp>";
			$this->load->view('products/goods_img', $this->view_data);
//		}
		$this->load->view('bottom');
	}

	// 상품이미지 임시폴더 업로드
	function goods_img_upload()
	{
		$goods_code = $this->uri->segment(3);
		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
		$goods_img_url = $this->config->item('user_goodscode_img_url').$goods_code.'/';

		// if($_SERVER["REMOTE_ADDR"] == "218.157.131.10")
		// {
		// 	// debug($_FILES['files']);exit;
		// 	if($this->compressImage($_FILES['files']['tmp_name'][0], $_FILES['files']['tmp_name'][0], 70))
		// 	{
		// 		debug($_FILES);exit;
		// 	}
		// }

		// [V1-HOTFIX-002] 동일 파일명 덮어쓰기: 기존 파일 선 삭제 후 재업로드 (2026.03.05)
		// 같은 파일명으로 재업로드 시 기존 원본·썸네일을 먼저 삭제하여 덮어쓰기 보장
		if (!empty($_FILES['files'])) {
			$upload_names = isset($_FILES['files']['name']) ? $_FILES['files']['name'] : array();
			if (!is_array($upload_names)) {
				$upload_names = array($upload_names);
			}
			foreach ($upload_names as $orig_name) {
				// Upload_handler 와 동일한 파일명 정규화 적용
				$clean_name = trim(basename(stripslashes((string)$orig_name)), ".\x00..\x20");
				$clean_name = preg_replace("/[\"\']/i", '', $clean_name);
				if ($clean_name) {
					$main_file = $goods_img_dir . $clean_name;
					if (is_file($main_file)) {
						@unlink($main_file);
					}
					$thumb_file = $goods_img_dir . 'thumbnail/' . $clean_name;
					if (is_file($thumb_file)) {
						@unlink($thumb_file);
					}
				}
			}
		}

		$data = array(
			'upload_dir' => $goods_img_dir,
			'upload_url' => $goods_img_url,
			'accept_file_types' => '/\.(mp4|gif|jpe?g|png)$/i',
			'image_versions' => array(
				// 원본 이미지 설정: 크기 제한 없음 (원본 크기 유지)
				'' => array(
					'auto_orient' => true,
					'jpeg_quality' => 75,
					'png_quality' => 9
				),
				'thumbnail' => array(
					'upload_dir' => $goods_img_dir.'thumbnail/',
					'upload_url' => $goods_img_url.'thumbnail/',
					'max_width' => 100,
					'max_height' => 100,
					'jpeg_quality' => 90,
					'png_quality' => 9,
					'crop' => true
				)
			)
		);
		
		$this->load->library('upload_handler', $data);
		$this->_ensure_thumbnails($goods_img_dir);

	}

	// 상품코드 이미지 순번정렬
	function goods_img_sorting()
	{
		if(!$this->uri->segment(3))
			alert_close('정상적인 접근이 아닙니다!');

		$this->load->helper('directory');

		$goods_code = $this->uri->segment(3);
		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
		$goods_img_url = $this->config->item('user_goodscode_img_url').$goods_code.'/thumbnail/';
		$this->view_data['goods_code'] = $goods_code;
		$this->view_data['img_url'] = $goods_img_url;
		$this->view_data['img_dir'] = $goods_img_dir.'thumbnail/';

		$sql = "SELECT
					GSD.GoodsSortImg
				FROM
					goods AS		GS LEFT OUTER JOIN
					goods_detail	As GSD ON GS.id = GSD.goods_id
				WHERE GS.GoodsCode='{$goods_code}'
		";
		$query = $this->db->query($sql);
		$row = $query->row();
		if (!$row) {
			alert_close('상품 정보를 찾을 수 없습니다! (코드: '.$goods_code.')');
			return;
		}
		$GoodsSortImg = $row->GoodsSortImg;
		$GoodsSortImgArr = explode('||', $GoodsSortImg);
		// debug($GoodsSortImgArr);exit;

		if(count($GoodsSortImgArr) > 0)
		{
			$this->view_data['imgs'] = $GoodsSortImgArr;
		}
		else {
			$map = directory_map($goods_img_dir);
			if (!isset($map['thumbnail']) || !is_array($map['thumbnail'])) {
				alert_close('정렬할 이미지가 없습니다!');
				return;
			}
			//debug($map);
			$this->view_data['imgs'] = $map['thumbnail'];
			// debug($this->view_data['imgs']);exit;
		}

		if(count($this->view_data['imgs']) > 0)
		{
			$this->load->helper('html');
			$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
			$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
			$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

			//debug($this->config_vars);
			// debug($this->view_data);

			$this->load->view('top_w', $this->view_data);
			$this->load->view('products/goods_img_sorting', $this->view_data);
			$this->load->view('bottom');
		}
		else
		alert_close('정렬할 이미지가 없습니다!');
	}

	// 상품코드 이미지 순번정렬1
	function goods_img_sorting_test1()
	{
		if(!$this->uri->segment(3))
			alert_close('정상적인 접근이 아닙니다!');

		$this->load->helper('directory');

		$goods_code = $this->uri->segment(3);
		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
		$goods_img_url = 'https://newtalk.kr'.$this->config->item('user_goodscode_img_url').$goods_code.'/thumbnail/';
		$this->view_data['goods_code'] = $goods_code;
		$this->view_data['img_url'] = $goods_img_url;
		$this->view_data['img_dir'] = $goods_img_dir.'thumbnail/';

		$thumbnail_dir = $goods_img_dir.'thumbnail/';
		$this->_ensure_thumbnails($goods_img_dir);
		$thumbnail_map = array();
		if(is_dir($thumbnail_dir)) {
			foreach(scandir($thumbnail_dir) as $thumbnail_file) {
				if($thumbnail_file == '.' || $thumbnail_file == '..') continue;
				if(is_file($thumbnail_dir.$thumbnail_file)) $thumbnail_map[] = $thumbnail_file;
			}
		}
		//debug($map);
		// $this->view_data['sortable1'] = $map['thumbnail'];
		$this->view_data['sortable1'] = [];

		$sql = "SELECT
					GS.id, GS.DeSkin,
					GSD.GoodsSortImg1, GSD.GoodsSortImg2, GSD.GoodsSortImg3
				FROM
					goods AS		GS LEFT OUTER JOIN
					goods_detail	As GSD ON GS.id = GSD.goods_id
				WHERE GS.GoodsCode='{$goods_code}'
		";
		$query = $this->db->query($sql);
		$row = $query->row();
		if (!$row) {
			alert_close('상품 정보를 찾을 수 없습니다! (코드: '.$goods_code.')');
			return;
		}
		$this->view_data['goodsId'] = $row->id;
		$this->view_data['DeSkin'] = $row->DeSkin;
		$GoodsSortImg1 = $row->GoodsSortImg1;
		$GoodsSortImg2 = $row->GoodsSortImg2;
		$GoodsSortImg3 = $row->GoodsSortImg3;
		$GoodsSortImgArr1 = explode('||', $GoodsSortImg1);
		$GoodsSortImgArr2 = explode('||', $GoodsSortImg2);
		$GoodsSortImgArr3 = explode('||', $GoodsSortImg3);
		// debug($GoodsSortImgArr);exit;

		$this->view_data['sortable2'] = $GoodsSortImgArr1;
		$this->view_data['sortable3'] = $GoodsSortImgArr2;
		$this->view_data['sortable4'] = $GoodsSortImgArr3;

		foreach ($thumbnail_map as $key => $value)
		{
			if(!in_array($value, array_merge($GoodsSortImgArr1, $GoodsSortImgArr2, $GoodsSortImgArr3))) $this->view_data['sortable1'][] = $value;
		}
		// debug($this->view_data['sortable1']);exit;

		if(count($thumbnail_map) > 0)
		{
			$this->load->library('mergearray_sort');
			$this->view_data['sortable1'] = $this->mergearray_sort->merge_sort_img($this->view_data['sortable1']);

			$ajax_limit = 50;
			$this->view_data['sortable1_total'] = count($this->view_data['sortable1']);
			$this->view_data['sortable2_total'] = count($this->view_data['sortable2']);
			$this->view_data['sortable3_total'] = count($this->view_data['sortable3']);
			$this->view_data['sortable4_total'] = count($this->view_data['sortable4']);
			$this->view_data['ajax_per_page'] = $ajax_limit;
			$this->view_data['sortable1'] = array_slice($this->view_data['sortable1'], 0, $ajax_limit);
			$this->view_data['sortable2'] = array_slice($this->view_data['sortable2'], 0, $ajax_limit);
			$this->view_data['sortable3'] = array_slice($this->view_data['sortable3'], 0, $ajax_limit);
			$this->view_data['sortable4'] = array_slice($this->view_data['sortable4'], 0, $ajax_limit);

			$this->load->helper('html');
			$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
			$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
			$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

			//debug($this->config_vars);
			// debug($this->view_data);

			// 오션 전송 마지막 로그 추출
			$this->load->model('goods_m');
			$this->view_data['ocean_last_date'] = $this->goods_m->get_goods_ocean_last_date($goods_code);
			$this->view_data['goods_info'] = $this->goods_m->get_goods_info($goods_code);

			$this->load->view('top_w', $this->view_data);
			// if($_SERVER["REMOTE_ADDR"] == "218.157.131.10")
				// $this->load->view('products/goods_img_sorting', $this->view_data);
			// else

//			if($_SERVER["REMOTE_ADDR"] == "121.134.129.200"){
//				$this->load->view('products/goods_img_sorting_test1_ocean_test', $this->view_data);
//			}else{
				$this->load->view('products/goods_img_sorting_test1', $this->view_data);
//			}
			$this->load->view('bottom');
		}
		else
			alert_close('정렬할 이미지가 없습니다!');
	}

	// 상품코드 이미지 순번정렬2
	function goods_img_sorting_test2()
	{
		if(!$this->uri->segment(3))
			alert_close('정상적인 접근이 아닙니다!');

		$this->load->helper('directory');

		$goods_code = $this->uri->segment(3);
		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
		$goods_img_url = 'https://newtalk.kr'.$this->config->item('user_goodscode_img_url').$goods_code.'/thumbnail/';
		$this->view_data['goods_code'] = $goods_code;
		$this->view_data['img_url'] = $goods_img_url;
		$this->view_data['img_dir'] = $goods_img_dir.'thumbnail/';

		$thumbnail_dir = $goods_img_dir.'thumbnail/';
		$this->_ensure_thumbnails($goods_img_dir);
		$thumbnail_map = array();
		if(is_dir($thumbnail_dir)) {
			foreach(scandir($thumbnail_dir) as $thumbnail_file) {
				if($thumbnail_file == '.' || $thumbnail_file == '..') continue;
				if(is_file($thumbnail_dir.$thumbnail_file)) $thumbnail_map[] = $thumbnail_file;
			}
		}
		//debug($map);
		// $this->view_data['sortable1'] = $map['thumbnail'];
		$this->view_data['sortable1'] = [];

		$sql = "SELECT
					GS.id, GS.MoSkin,
					GSD.GoodsSortImg4
				FROM
					goods AS		GS LEFT OUTER JOIN
					goods_detail	As GSD ON GS.id = GSD.goods_id
				WHERE GS.GoodsCode='{$goods_code}'
		";
		$query = $this->db->query($sql);
		$row = $query->row();
		if (!$row) {
			alert_close('상품 정보를 찾을 수 없습니다! (코드: '.$goods_code.')');
			return;
		}
		$this->view_data['goodsId'] = $row->id;
		$this->view_data['MoSkin'] = $row->MoSkin;
		$GoodsSortImg = $row->GoodsSortImg4;
		$GoodsSortImgArr = explode('||', $GoodsSortImg);
		// debug($GoodsSortImgArr);exit;

		$this->view_data['sortable2'] = $GoodsSortImgArr;

		foreach ($thumbnail_map as $key => $value)
		{
			if(!in_array($value, array_merge($GoodsSortImgArr))) $this->view_data['sortable1'][] = $value;
		}
		// debug($this->view_data['sortable1']);exit;

		if(count($thumbnail_map) > 0)
		{
			$this->load->library('mergearray_sort');
			$this->view_data['sortable1'] = $this->mergearray_sort->merge_sort_img($this->view_data['sortable1']);

			$ajax_limit = 50;
			$this->view_data['sortable1_total'] = count($this->view_data['sortable1']);
			$this->view_data['sortable2_total'] = count($this->view_data['sortable2']);
			$this->view_data['ajax_per_page'] = $ajax_limit;
			$this->view_data['sortable1'] = array_slice($this->view_data['sortable1'], 0, $ajax_limit);
			$this->view_data['sortable2'] = array_slice($this->view_data['sortable2'], 0, $ajax_limit);

			$this->load->helper('html');
			$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
			$this->view_data['link_tag2'] = link_tag('/assets/css/fileinput.min.css');
			$this->view_data['link_tag3'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

			//debug($this->config_vars);
			// debug($this->view_data);

			// 오션 전송 마지막 로그 추출
			$this->load->model('goods_m');
			$this->view_data['ocean_last_date'] = $this->goods_m->get_goods_ocean_last_date($goods_code);
			$this->view_data['goods_info'] = $this->goods_m->get_goods_info($goods_code);

			$this->load->view('top_w', $this->view_data);
			if($_SERVER["REMOTE_ADDR"] == "121.134.129.200"){
				$this->load->view('products/goods_img_sorting_test2_ocean_test', $this->view_data);
			}else{
				$this->load->view('products/goods_img_sorting_test2', $this->view_data);
			}
			$this->load->view('bottom');
		}
		else
			alert_close('정렬할 이미지가 없습니다!');
	}

	// 상품코드 이미지 파일수 저장
	function goods_img_cnt_save()
	{
		// 폼 데이타 변수 담기
		$goodscode = $this->input->post('goodscode');
		$filecnt = $this->input->post('filecnt');

        // 상품코드 이미지 로그 기록(2021.10.19)
		$status = $this->input->post('status');

		if($goodscode)
		{
            if($status)
            {
                $this->load->model('goods_m');

                // 상품코드 일련번호 가져오기
                $query = $this->db->query("SELECT id FROM goods WHERE GoodsCode='{$goodscode}'");
                $row = $query->row();
                $goods_id = $row->id;

                if($status == 'D') {
                    $d_query = $this->db->query("SELECT count(id) AS d_cnt FROM goods_action_logs WHERE goods_id='{$goods_id}' AND gb='D'");
                    $d_row = $d_query->row();
                    $d_cnt = $d_row->d_cnt;

                    // 상품 이미지 등록은 한번만
                    if($d_cnt < 1) {
                        // 상품 액션(등록) 로그
                        if($goods_id > 0)
                            $this->goods_m->action_logs($this->session->userdata('user_id'), $goods_id, $status);
                    }
                } else {
                    // 상품 액션(등록) 로그
                    if($goods_id > 0)
                        $this->goods_m->action_logs($this->session->userdata('user_id'), $goods_id, $status);
                }
            }

			$goods_data = array(
				'filecnt'	=> $filecnt,
				'filecnt_update	'	=> $this->info['created']
			);

			$this->db->where('gcode', $goodscode);
			$this->db->update('goods_code', $goods_data);
		}
	}

	function goods_img_sorting_api()
	{
		$goods_code = $this->input->get('goods_code');
		if (!$goods_code || !preg_match('/^[a-zA-Z0-9]+$/', $goods_code)) {
			header('Content-Type: application/json');
			echo json_encode(['error' => 'Invalid goods_code']);
			return;
		}

		$zone = $this->input->get('zone');
		$page = max(1, (int)$this->input->get('page'));
		$per_page = min(200, max(10, (int)$this->input->get('per_page') ?: 50));

		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
		$goods_img_url = 'https://newtalk.kr'.$this->config->item('user_goodscode_img_url').$goods_code.'/thumbnail/';
		$thumbnail_dir = $goods_img_dir.'thumbnail/';

		$this->_ensure_thumbnails($goods_img_dir);

		$thumbnail_map = array();
		if(is_dir($thumbnail_dir)) {
			foreach(scandir($thumbnail_dir) as $f) {
				if($f == '.' || $f == '..') continue;
				if(is_file($thumbnail_dir.$f)) $thumbnail_map[] = $f;
			}
		}

		$sql = "SELECT GS.id, GSD.GoodsSortImg1, GSD.GoodsSortImg2, GSD.GoodsSortImg3, GSD.GoodsSortImg4
				FROM goods AS GS LEFT OUTER JOIN goods_detail AS GSD ON GS.id = GSD.goods_id
				WHERE GS.GoodsCode='".$this->db->escape_str($goods_code)."'";
		$query = $this->db->query($sql);
		$row = $query->row();
		if (!$row) {
			header('Content-Type: application/json');
			echo json_encode(['error' => 'Product not found']);
			return;
		}

		$arr1 = array_filter(explode('||', $row->GoodsSortImg1));
		$arr2 = array_filter(explode('||', $row->GoodsSortImg2));
		$arr3 = array_filter(explode('||', $row->GoodsSortImg3));
		$arr4 = array_filter(explode('||', $row->GoodsSortImg4));

		$view = $this->input->get('view');
		$this->load->library('mergearray_sort');

		if ($view === 'test2') {
			$assigned_t2 = $arr4;
			$sortable1_t2 = [];
			foreach ($thumbnail_map as $f) {
				if (!in_array($f, $assigned_t2)) $sortable1_t2[] = $f;
			}
			$sortable1_t2 = $this->mergearray_sort->merge_sort_img($sortable1_t2);
			$zones = [
				'sortable1' => $sortable1_t2,
				'sortable2' => array_values($arr4),
			];
		} else {
			$assigned = array_merge($arr1, $arr2, $arr3);
			$sortable1 = [];
			foreach ($thumbnail_map as $f) {
				if (!in_array($f, $assigned)) $sortable1[] = $f;
			}
			$sortable1 = $this->mergearray_sort->merge_sort_img($sortable1);
			$zones = [
				'sortable1' => $sortable1,
				'sortable2' => array_values($arr1),
				'sortable3' => array_values($arr2),
				'sortable4' => array_values($arr3),
			];
		}

		if (!isset($zones[$zone])) {
			header('Content-Type: application/json');
			echo json_encode(['error' => 'Invalid zone']);
			return;
		}

		$all = array_values($zones[$zone]);
		$total = count($all);
		$offset = ($page - 1) * $per_page;
		$items = array_slice($all, $offset, $per_page);

		$result = [];
		foreach ($items as $name) {
			if (!$name) continue;
			$file = $thumbnail_dir . $name;
			$parts = explode('.', $name);
			$result[] = [
				'name' => $name,
				'base' => $parts[0],
				'ext' => isset($parts[1]) ? $parts[1] : '',
				'src' => $goods_img_url . $name . (is_file($file) ? '?v='.filemtime($file) : ''),
				'exists' => is_file($file),
			];
		}

		header('Content-Type: application/json');
		echo json_encode([
			'items' => $result,
			'total' => $total,
			'page' => $page,
			'per_page' => $per_page,
		]);
	}

	private function _ensure_thumbnails($goods_img_dir)
	{
		$thumbnail_dir = $goods_img_dir . 'thumbnail/';

		if (is_dir($thumbnail_dir)) {
			$existing = glob($thumbnail_dir . '*.{jpg,jpeg,png,gif,JPG,JPEG,PNG,GIF}', GLOB_BRACE);
			if (!empty($existing)) return;
		}

		$images = glob($goods_img_dir . '*.{jpg,jpeg,png,gif,JPG,JPEG,PNG,GIF}', GLOB_BRACE);
		if (empty($images)) return;

		if (!is_dir($thumbnail_dir)) {
			if (!@mkdir($thumbnail_dir, 0755, true)) return;
			@chown($thumbnail_dir, 'danharoo');
			@chgrp($thumbnail_dir, 'danharoo');
		}

		$max_dim = 100;
		foreach ($images as $source) {
			$filename = basename($source);
			$dest = $thumbnail_dir . $filename;
			if (file_exists($dest)) continue;

			$info = @getimagesize($source);
			if (!$info) continue;

			switch ($info['mime']) {
				case 'image/jpeg': $src = @imagecreatefromjpeg($source); break;
				case 'image/png':  $src = @imagecreatefrompng($source); break;
				case 'image/gif':  $src = @imagecreatefromgif($source); break;
				default: continue 2;
			}
			if (!$src) continue;

			$w = imagesx($src); $h = imagesy($src);
			if ($w > $h) { $nw = $max_dim; $nh = max(10, (int)($h * $max_dim / $w)); }
			else         { $nh = $max_dim; $nw = max(10, (int)($w * $max_dim / $h)); }

			$thumb = imagecreatetruecolor($nw, $nh);
			if ($info['mime'] === 'image/png') {
				imagealphablending($thumb, false);
				imagesavealpha($thumb, true);
			}
			imagecopyresampled($thumb, $src, 0, 0, 0, 0, $nw, $nh, $w, $h);

			switch ($info['mime']) {
				case 'image/jpeg': imageinterlace($thumb, 1); imagejpeg($thumb, $dest, 90); break;
				case 'image/png':  imagepng($thumb, $dest, 9); break;
				case 'image/gif':  imagegif($thumb, $dest); break;
			}
			imagedestroy($src);
			imagedestroy($thumb);
			@chmod($dest, 0644);
		}
	}

	// 상품코드 이미지 정렬 데이타 비교 후 삭제 이미지 정리
	function goods_img_sorting_delete()
	{
		$this->load->helper('directory');

		// 폼 데이타 변수 담기
		$data = $this->input->post();
		// debug($data);

		$sortable1 = $sortable2 = $sortable3 = [];

		$query = $this->db->query("SELECT GoodsSortImg1, GoodsSortImg2, GoodsSortImg3 FROM goods_detail WHERE goods_id='{$data['goodsId']}'");
		$row = $query->row();
		// debug($row);
		$GoodsSortImgArr1 = explode('||', $row->GoodsSortImg1);
		$GoodsSortImgArr2 = explode('||', $row->GoodsSortImg2);
		$GoodsSortImgArr3 = explode('||', $row->GoodsSortImg3);
		// debug($GoodsSortImgArr);

		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$data['goodsCode'].'/';
		$goods_img_url = $this->config->item('user_goodscode_img_url').$data['goodsCode'].'/thumbnail/';

		$map = directory_map($goods_img_dir);
		// debug($map['thumbnail']);

		foreach ($map['thumbnail'] as $key => $value)
		{
			if(in_array($value, $GoodsSortImgArr1)) $sortable1[] = $value;
			if(in_array($value, $GoodsSortImgArr2)) $sortable2[] = $value;
			if(in_array($value, $GoodsSortImgArr3)) $sortable3[] = $value;
		}

		// debug($sortable1);exit;

		$sortable1_txt = implode('||', $sortable1);
		$sortable2_txt = implode('||', $sortable2);
		$sortable3_txt = implode('||', $sortable3);
		// debug($sortable_txt);exit;
		// if(!$sortable_txt) $sortable_txt = '';

		$goods_data = array(
			'GoodsSortImg1'	=> $sortable1_txt,
			'GoodsSortImg2'	=> $sortable2_txt,
			'GoodsSortImg3'	=> $sortable3_txt
		);
		// debug($goods_data);exit;
		$this->db->where('goods_id', $data['goodsId']);
		$this->db->update('goods_detail', $goods_data);

		echo '{"info":{"success":true,"text":"처리되었습니다!"}}';
	}

	// 상품코드 이미지 순번정렬1 저장 수정(2020.11.09)
	private function _filter_existing_sort_images($goods_code, $image_list)
	{
		$thumbnail_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/thumbnail/';
		if(!is_dir($thumbnail_dir)) return '';

		$result = array();
		$items = explode('||', (string)$image_list);
		foreach($items as $item)
		{
			$filename = trim($item);
			if($filename === '') continue;
			if(basename($filename) !== $filename) continue;
			if(!is_file($thumbnail_dir.$filename)) continue;
			if(!in_array($filename, $result, true)) $result[] = $filename;
		}
		return count($result) > 0 ? implode('||', $result).'||' : '';
	}

	function goods_img_sorting_save1()
	{
		// 폼 데이타 변수 담기
		$data = $this->input->post();
		// debug($data);exit;
		$query = $this->db->query("SELECT id FROM goods WHERE GoodsCode='{$data['GoodsCode']}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();
		$GoodsId = $row->id;
		if(!$GoodsId || $GoodsId < 1)
		{
			echo '{"info":{"success":false,"text":"해당 상품코드와 매칭되는 상품정보가 없습니다!"}}';
			exit;
		}
		$data['GoodsImageList1'] = $this->_filter_existing_sort_images($data['GoodsCode'], isset($data['GoodsImageList1']) ? $data['GoodsImageList1'] : '');
		$data['GoodsImageList2'] = $this->_filter_existing_sort_images($data['GoodsCode'], isset($data['GoodsImageList2']) ? $data['GoodsImageList2'] : '');
		$data['GoodsImageList3'] = $this->_filter_existing_sort_images($data['GoodsCode'], isset($data['GoodsImageList3']) ? $data['GoodsImageList3'] : '');
		$goods_data = array(
			'GoodsSortImg1'	=> $data['GoodsImageList1'],	// 인트로
			'GoodsSortImg2'	=> $data['GoodsImageList2'],	// 모델사진
			'GoodsSortImg3'	=> $data['GoodsImageList3'],	// 제품사진
		);
		$this->db->where('goods_id', $GoodsId);
		$this->db->update('goods_detail', $goods_data);
		
		// =================================================================
		// Skin8 자동 분할을 위한 세션 저장 추가 (2026-02-02)
		// =================================================================
		$sessionKey = 'goods_sorting_' . $GoodsId;
		$sortingData = array(
			'goodsId' => $GoodsId,
			'goodsCode' => $data['GoodsCode'],
			'model_images' => !empty($data['GoodsImageList2']) ? explode('||', $data['GoodsImageList2']) : array(),
			'intro_images' => !empty($data['GoodsImageList1']) ? explode('||', $data['GoodsImageList1']) : array(),
			'product_images' => !empty($data['GoodsImageList3']) ? explode('||', $data['GoodsImageList3']) : array(),
			'timestamp' => time(),
		);
		$this->session->set_userdata($sessionKey, $sortingData);
		// =================================================================
		
		echo '{"info":{"success":true,"text":"처리되었습니다!"}}';
	}

	function goods_img_sorting_save2()
	{
		// debug($data);exit;
		// 폼 데이타 변수 담기
		$data = $this->input->post();
		// debug($data);exit;

		$query = $this->db->query("SELECT id FROM goods WHERE GoodsCode='{$data['GoodsCode']}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$GoodsId = $row->id;

		if(!$GoodsId || $GoodsId < 1)
		{
			echo '{"info":{"success":false,"text":"해당 상품코드와 매칭되는 상품정보가 없습니다!"}}';
			exit;
		}
		$data['GoodsImageList4'] = $this->_filter_existing_sort_images($data['GoodsCode'], isset($data['GoodsImageList4']) ? $data['GoodsImageList4'] : '');

		$goods_data = array(
			'GoodsSortImg4'	=> $data['GoodsImageList4'],	// MO
		);
		$this->db->where('goods_id', $GoodsId);
		$this->db->update('goods_detail', $goods_data);

		echo '{"info":{"success":true,"text":"처리되었습니다!"}}';
	}

	// 상품이미지 임시폴더 관리
	function temp_img()
	{
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/assets/css/jquery.fileupload.css');
		$this->view_data['link_tag3'] = link_tag('/assets/css/jquery.fileupload-ui.css');
		$this->view_data['link_tag4'] = link_tag('/assets/css/ladda-themeless.min.css');
		$this->view_data['link_tag5'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$this->load->view('top', $this->view_data);
		$this->load->view('products/temp_img.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품이미지 임시폴더 업로드
	function temp_upload()
	{
		//$user_id = $this->session->userdata('user_id');
		//$goods_code = $this->uri->segment(3);

		$code = $this->input->get('code');

		//debug($file);

		//$files =  $file[0];
		$goods_img_dir = $this->config->item('user_goodscode_img_dir').$code.'/';

		$data = array(
			'upload_dir' => $goods_img_dir,
			'accept_file_types' => '/\.(gif|jpe?g|png)$/i',
			//'file' => $files,
		);
		$this->load->library('upload_handler', $data);

		//debug($this->upload_handler);
	}

	// 상품대량등록
	function excel()
	{
		$user_id = $this->session->userdata('user_id');

		// 마켓목록
		$query = $this->db->query("SELECT * FROM user_market WHERE user_id='{$user_id}' ORDER BY market ASC");
		$this->view_data['market'] = $query->result();

		if($query->num_rows() < 1) alert('등록된 마켓 계정이 없습니다.\n\n등록된 마켓 계정이 한 곳이상 있어야 가능합니다!', '/setting/market');

		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		$this->view_data['link_tag4'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$this->load->view('top', $this->view_data);
		//$this->load->view('products/excel', $this->view_data);
		$this->load->view('products/excel_thread', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품대량등록 체크
	function excel_master_check()
	{
		$user_id = $this->session->userdata('user_id');
		//debug($data);debug($_FILES);exit;

		$temp_img_path = '/data/files/goods/'.$user_id.'/';

		$config['upload_path'] = '/home/ubuntu/data/files/excel/'.$user_id.'/';
		if(!is_dir($config['upload_path'])) mkdir($config['upload_path'], 0755, TRUE);
		$config['allowed_types'] = 'xls';
		$this->load->library('upload', $config);

		if( ! $this->upload->do_upload('excel_file'))
		{
			$error = array('error' => $this->upload->display_errors());
			//debug($error);
			$rtn_data_json = json_encode($error);
			echo $rtn_data_json;
		}
		else
		{
			$data = array('upload_data' => $this->upload->data());
			//debug($data);

			$excel_data = array();

			$this->load->library('excel');
			$objPHPExcel = PHPExcel_IOFactory::load($data['upload_data']['full_path']);
			$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
			//debug($sheetDataArr);

			$img_cell_arr = array("D", "N", "O", "P", "Q");

			foreach($sheetDataArr AS $k1 => $v1_arr)
			{
				if($k1 > 3)
				{
					//debug($v1);
					// 이미지 경로 처리
					/*
					foreach($img_cell_arr AS $k2 => $v2)
					{
						if($v1_arr[$v2] !== null)
							$sheetDataArr[$k1][$v2] = $temp_img_path.$v1_arr[$v2];
						else
							$sheetDataArr[$k1][$v2] = '';
						//debug($v1_arr[$v2]);
					}
					*/

					if($v1_arr['B'] != null && $v1_arr['C'] != null && $v1_arr['D'] != null)
					{
						foreach($v1_arr AS $k3 => $v3)
						{
							if($v3 == null) $sheetDataArr[$k1][$k3] = '';
						}

						$excel_data[] = $sheetDataArr[$k1];
					}
				}
			}

			if(count($excel_data) < 1)
			{
				$error = array('error' => '엑셀 파일에 등록된 상품이 없거나 필수 항목이 누락된 상품입니다.');
				//debug($error);
				$rtn_data_json = json_encode($error);
				echo $rtn_data_json;
				exit;
			}
			//debug($excel_data);

			// 데이타별 마켓 상품 등록
			$master_data = array();
			foreach($excel_data AS $k => $v_arr)
			{
				$v_arr_json = str_replace('\n', '##', json_encode($v_arr));
				//debug($v_arr_json);
				$v_arr = json_decode($v_arr_json, 1);

				//debug($v_arr);
				$market_data = array();
				$market_data['GoodsCmd'] = '1';
				$market_data['DataRtn'] = '1';

				$market_data['GoodsImageList'] = '';
				// 이미지 파일 처리
				$GoodsImageListArr = array();
				foreach($img_cell_arr AS $k4 => $v4)
				{
					if($v_arr[$v4] != '')
					{
						$GoodsImageListArr[] = $v_arr[$v4];
						$excel_data[$k][$v4] = $temp_img_path.$v_arr[$v4];
					}
				}
				$market_data['GoodsImageList'] = implode('||', $GoodsImageListArr);

				$market_data['Category1'] = array("");
				$market_data['Category2'] = array("");
				$market_data['Category3'][0] = 'G|'.$v_arr['B'];
				$market_data['Category4'] = array("");

				$market_data['GoodsPrice'] = $v_arr['G'];
				$market_data['GoodsName'] = $v_arr['C'];
				$market_data['CatalogName'] = '';
				$market_data['BrandName'] = '';
				$market_data['MakerName'] = '';

				//$market_data['SellingPeriodStart'] = date('Y-m-d', time());
				//$start_day = $market_data['SellingPeriodStart'];
				//$end_day = date("Y-m-d", strtotime ("+".$v_arr['F']." days"));

				//$market_data['SellingPeriod'] = $start_day.'|'.$end_day.'|'.$v_arr['F'];

				$market_data['GoodsPrice'] = $v_arr['F'];
				$market_data['OptionColor'] = $v_arr['G'];
				$market_data['OptionSize'] = $v_arr['H'];
				$market_data['OptionEtc'] = $v_arr['I'];
				$market_data['OpenWho'] = $v_arr['J'];
				$market_data['AfterDays'] = $v_arr['K'];
				$market_data['MadeIn'] = $v_arr['L'];
				$market_data['StyleW'] = $v_arr['M'];

				$market_data['GoodsCount'] = '1';

				$market_data['GoodsOptionsUseSetting'] = 'N';

				$market_data['Description'] = $v_arr['E'];
				$market_data['CommonDeliveryWayOPTSEL'] = '';
				$market_data['DeliveryCOMP'] = '';
				$market_data['ShipmentPlaceNo'] = '';
				$market_data['DeliveryFeeType'] = '';
				$market_data['NoticeItemGroupNo'] = '';
				//debug($market_data);exit;

				$master_data[$k] = $this->update_master($market_data);
			}

			$rtn_data_json = json_encode($master_data);
			echo $rtn_data_json;
		}

		delete_files($config['upload_path'], true);
	}

	// 상품대량등록 체크
	function excel_check()
	{
		$user_id = $this->session->userdata('user_id');
		$market = $this->input->post('market');
		$master = $this->input->post('master');
		$master_arr = explode(',', $master);
		//debug($master);debug($_FILES);exit;

		$temp_img_path = '/data/files/goods/'.$user_id.'/';

		$config['upload_path'] = '/home/ubuntu/data/files/excel/'.$user_id.'/';
		if(!is_dir($config['upload_path'])) mkdir($config['upload_path'], 0755, TRUE);
		$config['allowed_types'] = 'xls';
		$this->load->library('upload', $config);

		if( ! $this->upload->do_upload('excel_file'))
		{
			$error = array('error' => $this->upload->display_errors());
			//debug($error);
			$rtn_data_json = json_encode($error);
			echo $rtn_data_json;
		}
		else
		{
			$data = array('upload_data' => $this->upload->data());
			//debug($data);

			$excel_data = array();

			$this->load->library('excel');
			$objPHPExcel = PHPExcel_IOFactory::load($data['upload_data']['full_path']);
			$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
			//debug($sheetDataArr);

			$img_cell_arr = array("D", "N", "O", "P", "Q");

			foreach($sheetDataArr AS $k1 => $v1_arr)
			{
				if($k1 > 3)
				{
					//debug($v1);
					// 이미지 경로 처리
					/*
					foreach($img_cell_arr AS $k2 => $v2)
					{
						if($v1_arr[$v2] !== null)
							$sheetDataArr[$k1][$v2] = $temp_img_path.$v1_arr[$v2];
						else
							$sheetDataArr[$k1][$v2] = '';
						//debug($v1_arr[$v2]);
					}
					*/

					if($v1_arr['B'] != null && $v1_arr['C'] != null && $v1_arr['D'] != null)
					{
						foreach($v1_arr AS $k3 => $v3)
						{
							if($v3 == null) $sheetDataArr[$k1][$k3] = '';
						}

						$excel_data[] = $sheetDataArr[$k1];
					}
				}
			}

			if(count($excel_data) < 1)
			{
				$error = array('error' => '엑셀 파일에 등록된 상품이 없거나 필수 항목이 누락된 상품입니다.');
				//debug($error);
				$rtn_data_json = json_encode($error);
				echo $rtn_data_json;
				exit;
			}
			//debug($excel_data);

			// 데이타별 마켓 상품 등록
			//$market_data = array();
			foreach($excel_data AS $k => $v_arr)
			{
				$v_arr_json = str_replace('\n', '##', json_encode($v_arr));
				//debug($v_arr_json);
				$v_arr = json_decode($v_arr_json, 1);

				//debug($v_arr);
				$market_data = array();
				$market_data['GoodsCmd'] = '1';
				$market_data['GoodsExcel'] = '1';
				$market_data['GdsMstId'] = $master_arr[$k];

				$market_data['GoodsImageList'] = '';
				// 이미지 파일 처리
				$GoodsImageListArr = array();
				foreach($img_cell_arr AS $k4 => $v4)
				{
					if($v_arr[$v4] != '')
					{
						$GoodsImageListArr[] = $v_arr[$v4];
						$excel_data[$k][$v4] = $temp_img_path.$v_arr[$v4];
					}
				}
				$market_data['GoodsImageList'] = implode('||', $GoodsImageListArr);

				if($market)
					$market_data['SellType'] = array($market);
				else
					$market_data['SellType'] = array("G");
				//debug($market_data['SellType']);exit;
				//$market_data['SellType'] = array("A", "B");
				$market_data['Category1'] = array("");
				$market_data['Category2'] = array("");
				$market_data['Category3'][0] = 'G|'.$v_arr['B'];
				$market_data['Category4'] = array("");

				$market_data['GoodsName'] = $v_arr['C'];
				$market_data['CatalogName'] = '';
				$market_data['BrandName'] = '';
				$market_data['MakerName'] = '';

				//$market_data['SellingPeriodStart'] = date('Y-m-d', time());
				//$start_day = $market_data['SellingPeriodStart'];
				//$end_day = date("Y-m-d", strtotime ("+".$v_arr['F']." days"));

				//$market_data['SellingPeriod'] = $start_day.'|'.$end_day.'|'.$v_arr['F'];

				$market_data['GoodsPrice'] = $v_arr['F'];
				$market_data['OptionColor'] = $v_arr['G'];
				$market_data['OptionSize'] = $v_arr['H'];
				$market_data['OptionEtc'] = $v_arr['I'];
				$market_data['OpenWho'] = $v_arr['J'];
				$market_data['AfterDays'] = $v_arr['K'];
				$market_data['MadeIn'] = $v_arr['L'];
				$market_data['StyleW'] = $v_arr['M'];

				$market_data['GoodsCount'] = '1';

				$market_data['GoodsOptionsUseSetting'] = 'N';

				/*
				$OptNameArr = explode('##', $v_arr['I']);
				$OptValueArr = explode('##', $v_arr['J']);
				$OptPriceArr = explode('|', $v_arr['K']);
				$OptStockArr = explode('|', $v_arr['L']);
				//debug($v_arr['I']);
				//debug($OptNameArr);
				//debug($OptValueArr);

				$goodsOptData = array();
				foreach($OptNameArr AS $k1 => $v1)
				{
					foreach($OptValueArr AS $k2 => $v2)
					{
						$v2_arr = explode('|', $v2);
						foreach($v2_arr AS $k3 => $v3)
						{
							$goodsOptData[] = '{"OptName": "'.$v1.'", "OptValue": "'.$v3.'", "OptPrice": "'.$OptPriceArr[$k3].'", "OptStock": "'.$OptStockArr[$k3].'"}';
						}
					}
				}
				//debug($goodsOptData);

				$market_data['GoodsOptVal'] = '['.implode(',', $goodsOptData).']';
				*/
				$market_data['GoodsOptVal'] = '';

				$market_data['Description'] = $v_arr['E'];
				$market_data['CommonDeliveryWayOPTSEL'] = '';
				$market_data['DeliveryCOMP'] = '';
				$market_data['ShipmentPlaceNo'] = '';
				$market_data['DeliveryFeeType'] = '';
				$market_data['NoticeItemGroupNo'] = '';
				$market_data['NoticeItemCodes'] = '';
				//debug($market_data);exit;

				$rtn_market_data = $this->update_process($market_data);
				$rtn_market_data_josn = json_decode($rtn_market_data, 1);

				// 실패
				if($rtn_market_data_josn['info']['success'] === false)
				{
					// {"info":{"success":false,"error":{"kind":"img","key":"A","msg":"이미지 데이타가 없습니다."}}}
					$kind = $rtn_market_data_josn['info']['error']['kind'];
					$key = $rtn_market_data_josn['info']['error']['key'];
					$msg = $rtn_market_data_josn['info']['error']['msg'];

					switch($kind)
					{
						case "img": $kind = '이미지 에러'; break;
						case "db": $kind = '디비 에러'; break;
					}
					$excel_data[$k]['R'] = '['.$kind.']\n'.$msg;
				}
				// 성공
				else if($rtn_market_data_josn['info']['success'] === true)
					$excel_data[$k]['R'] = '성공';
				else
					$excel_data[$k]['R'] = '실패';

				$excel_data[$k]['MKGB'] = $market;
				$excel_data[$k]['MKNM'] = $this->config_vars['open_market2'][$market];
			}

			$rtn_data_json = str_replace('\n', '<br />', json_encode($excel_data));
			$rtn_data_json = str_replace('null', '', $rtn_data_json);
			echo $rtn_data_json;
		}

		delete_files($config['upload_path'], true);
	}

	// 뉴톡 상품 대량 수정(2018.10.21)
	function excel_update()
	{
		$user_id = $this->session->userdata('user_id');

		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		$this->view_data['link_tag4'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$this->load->view('top', $this->view_data);
		//$this->load->view('products/excel', $this->view_data);
		$this->load->view('products/excel_update', $this->view_data);
		$this->load->view('bottom');
	}

	// 뉴톡 상품 대량 수정 체크(2018.10.21)
	function excel_update_check()
	{
		$user_id = $this->session->userdata('user_id');

		$temp_img_path = '/data/files/goods/'.$user_id.'/';

		$config['upload_path'] = '/home/danharoo/www/data/files/excel/'.$user_id.'/';
		if(!is_dir($config['upload_path'])) mkdir($config['upload_path'], 0755, TRUE);
		$config['allowed_types'] = '*';
		$this->load->library('upload', $config);

		if( ! $this->upload->do_upload('excel_file'))
		{
			$error = array('error' => $this->upload->display_errors());
			//debug($error);
			$rtn_data_json = json_encode($error);
			echo $rtn_data_json;
		}
		else
		{
			$data = array('upload_data' => $this->upload->data());
			//debug($data);

			$excel_data = array();

			$this->load->library('excel');
			$objPHPExcel = PHPExcel_IOFactory::load($data['upload_data']['full_path']);
			$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
			//debug($sheetDataArr);exit;

			foreach($sheetDataArr AS $k1 => $v1_arr)
			{
				if($k1 > 1)
				{
					if($v1_arr['CA'] != null)
					{
						foreach($v1_arr AS $k3 => $v3)
						{
							if($v3 == null) $sheetDataArr[$k1][$k3] = '';
						}

						$excel_data[] = $sheetDataArr[$k1];
					}
				}
			}

			if(count($excel_data) < 1)
			{
				$error = array('error' => '엑셀 파일에 등록된 상품이 없거나 필수 항목이 누락된 상품입니다.');
				//debug($error);
				$rtn_data_json = json_encode($error);
				echo $rtn_data_json;
				exit;
			}
			//debug($excel_data);exit;

			// 데이타별 마켓 상품 등록
			$rtn_data = array();
			$master_data = array();
			foreach($excel_data AS $k => $v_arr)
			{
				$v_arr_json = json_encode($v_arr);
				//debug($v_arr_json);
				$v_arr = json_decode($v_arr_json, 1);

				//debug($v_arr);
				$market_data = array();
				$market_data['GoodsCmd'] = '2';
				$market_data['GoodsExcel'] = '1';
				$market_data['GoodsImageList'] = '';
				$market_data['GoodsImageEtcList'] = '';
				$market_data['GoodsCode_6'] = '';
				$market_data['GoodsMovieUrl'] = '';

				$market_data['market'] = $v_arr['A'];

				// 상품분류 (대>중>소>세)
				$market_data['Category1'] = '';
				$market_data['Category2'] = '';
				$market_data['Category3'] = '';

				$goods_cate1 = $v_arr['B'];
				$goods_cate2 = $v_arr['C'];
				$goods_cate3 = $v_arr['D'];

				if($goods_cate1)
				{
					$query1 = $this->db->query("SELECT id FROM goods_cate WHERE market='H' AND LargeCategory='{$goods_cate1}' AND activated='Y' ORDER BY id ASC limit 1");
					if($query1->num_rows() > 0) $cate1 = $query1->row();

					if(isset($cate1) && $cate1->id) $market_data['Category1'] = $cate1->id;
				}
				if($goods_cate2)
				{
					$query2 = $this->db->query("SELECT id FROM goods_cate WHERE market='H' AND LargeCategory='{$goods_cate1}' AND MiddleCategory='{$goods_cate2}' AND activated='Y' ORDER BY id ASC limit 1");
					if($query2->num_rows() > 0) $cate2 = $query2->row();

					if(isset($cate2) && $cate2->id) $market_data['Category2'] = $cate2->id.'|1';
				}
				if($goods_cate3)
				{
					$query3 = $this->db->query("SELECT id FROM goods_cate WHERE market='H' AND LargeCategory='{$goods_cate1}' AND MiddleCategory='{$goods_cate2}' AND SmallCategory='{$goods_cate3}' AND activated='Y' ORDER BY id ASC limit 1");
					if($query3->num_rows() > 0) $cate3 = $query3->row();

					if(isset($cate3) && $cate3->id) $market_data['Category3'] = $cate3->id.'|2';
				}

				$market_data['activated'] = $v_arr['E'];
				if($market_data['activated'] == 'Y') {
					$market_data['activated_day'] = date('Y-m-d', time());
				}

				// 이미지 파일 처리(엑셀 수정에서 미반영 수정 - 2021.05.26)
				// $market_data['GoodsImageList'] = '';
				// $GoodsImageListArr = array();
				// $GoodsImageListArr[] = $v_arr['F'];
				// $market_data['GoodsImageList'] = implode('||', $GoodsImageListArr);

				$market_data['GoodsCode'] = $v_arr['G'];
				$market_data['GoodsName'] = $v_arr['H'];
				$market_data['GoodsEtc4'] = $v_arr['I'];
				$market_data['GoodsEtc7'] = $v_arr['J'];
				$market_data['GoodsEtc8'] = $v_arr['K'];
				$market_data['GoodsEtc5'] = $v_arr['L'];
				$market_data['GoodsEtc6'] = $v_arr['M'];
				$market_data['GoodsEtc9'] = ($v_arr['N'] !== '' ? intval($v_arr['N']) : 0);
				$market_data['GoodsEtc10'] = ($v_arr['O'] !== '' ? intval($v_arr['O']) : 0);
				$market_data['GoodsPrice'] = ($v_arr['P'] !== '' ? intval($v_arr['P']) : 0);
				$market_data['GoodsEtc32'] = ($v_arr['Q'] !== '' ? intval($v_arr['Q']) : 0);
				$market_data['GoodsEtc35'] = ($v_arr['R'] !== '' ? intval($v_arr['R']) : 0);
				$market_data['GoodsEtc33'] = ($v_arr['S'] !== '' ? intval($v_arr['S']) : 0);
				$market_data['GoodsEtc34'] = ($v_arr['T'] !== '' ? intval($v_arr['T']) : 0);
				$market_data['GoodsEtc41'] = ($v_arr['U'] !== '' ? intval($v_arr['U']) : 0);
				$market_data['GoodsEtc48'] = $v_arr['V'];
				$market_data['GoodsEtc51'] = $v_arr['W'];
				$market_data['GoodsEtc52'] = $v_arr['X'];
				$market_data['Description'] = $v_arr['Y'];
				$market_data['GoodsEtc24'] = $v_arr['Z'];

				$market_data['GoodsEtc25'] = $v_arr['AA'];
				$market_data['GoodsEtc26'] = $v_arr['AB'];
				$market_data['OptionColor'] = $v_arr['AC'];
				$market_data['OptionSize'] = $v_arr['AD'];
				$market_data['SocialGoodsOption'] = $v_arr['AE'];
				$market_data['GoodsEtc16'] = $v_arr['AF'];
				$market_data['GoodsEtc14'] = $v_arr['AG'];
				$market_data['GoodsEtc13'] = $v_arr['AH'];
				$market_data['GoodsEtc15'] = $v_arr['AI'];
				$market_data['GoodsEtc18'] = $v_arr['AJ'];
				$market_data['GoodsEtc17'] = $v_arr['AK'];
				$market_data['MakerName'] = $v_arr['AL'];
				$market_data['GoodsEtc20'] = $v_arr['AM'];
				$market_data['GoodsEtc21'] = $v_arr['AN'];
				$market_data['GoodsEtc22'] = $v_arr['AO'];
				$market_data['GoodsEtc27'] = $v_arr['AP'];
				$market_data['GoodsEtc28'] = $v_arr['AQ'];
				$market_data['GoodsEtc29'] = $v_arr['AR'];
				$market_data['GoodsEtc30'] = $v_arr['AS'];
				$market_data['GoodsEtc58'] = $v_arr['AT'];
				$market_data['GoodsEtc59'] = $v_arr['AU'];
				$market_data['GoodsEtc36'] = $v_arr['AV'];
				$market_data['GoodsEtc37'] = $v_arr['AW'];
				$market_data['GoodsEtc38'] = $v_arr['AX'];
				$market_data['GoodsEtc39'] = $v_arr['AY'];
				$market_data['GoodsEtc40'] = $v_arr['AZ'];

				$market_data['SellingPeriod'] = $v_arr['BA'];
				$market_data['GoodsCount'] = $v_arr['BB'];
				$market_data['GoodsEtc56'] = $v_arr['BC'];
				$market_data['GoodsEtc57'] = $v_arr['BD'];
				$market_data['GoodsEtc53'] = $v_arr['BE'];
				$market_data['GoodsEtc54'] = $v_arr['BF'];
				$market_data['GoodsEtc55'] = $v_arr['BG'];
				$market_data['GoodsEtc73'] = $v_arr['BH'];
				$market_data['GoodsEtc60'] = $v_arr['BI'];
				$market_data['GoodsEtc61'] = $v_arr['BJ'];
				$market_data['GoodsEtc62'] = $v_arr['BK'];
				$market_data['GoodsEtc63'] = $v_arr['BL'];
				$market_data['GoodsEtc64'] = $v_arr['BM'];
				$market_data['GoodsEtc65'] = $v_arr['BN'];
				$market_data['GoodsEtc66'] = $v_arr['BO'];
				$market_data['GoodsEtc67'] = $v_arr['BP'];
				$market_data['GoodsEtc68'] = $v_arr['BQ'];
				$market_data['GoodsEtc69'] = $v_arr['BR'];
				$market_data['GoodsEtc70'] = $v_arr['BS'];
				$market_data['GoodsEtc74'] = $v_arr['BT'];
				$market_data['GoodsEtc71'] = $v_arr['BU'];
				$market_data['GoodsEtc72'] = $v_arr['BV'];

				// 2019.09.23 추가
				// 미니몰노출여부
				$market_data['mall_activated'] = $v_arr['BW'] ? trim($v_arr['BW']) : '';
				$market_data['DanharooGoodsName'] = $v_arr['BX'] ? trim($v_arr['BX']) : '';

				// 2019.12.19 추가
				// 중국옵션
				$market_data['OptionColorChina'] = $v_arr['BY'] ? trim($v_arr['BY']) : '';
				$market_data['OptionSizeChina'] = $v_arr['BZ'] ? trim($v_arr['BZ']) : '';

				// 2020.05.13 추가
				// 단하루상품설명
				$market_data['DanharooDescription'] = $v_arr['CA'];

				$market_data['GoodsId'] = $v_arr['CB']; // 상품수정고유번호(삭제/수정불가)
					// DanharooDescription

				// 2020.05.28 추가
				// 브랜드명
				$market_data['BrandName'] = $v_arr['CC'];

				// 2020.06.01 추가
				// 단독상품
				$market_data['GoodsOnly'] = $v_arr['CD'];

				//debug($market_data);exit;

				$rtn_market_data = $this->update_process($market_data);
				$rtn_market_data_josn = json_decode($rtn_market_data, 1);

				$rtn_data[$k]['A'] = $v_arr['A'];	// 마켓
				$rtn_data[$k]['B'] = $v_arr['B'].' > '.$v_arr['C'].' > '.$v_arr['D'];	// 카테고리
				$rtn_data[$k]['C'] = $v_arr['G'];	// 상품코드
				$rtn_data[$k]['D'] = $v_arr['H'];	// 상품명
				$rtn_data[$k]['E'] = '/data/files/goods/5/thumbnail/'.$v_arr['F'];	// 대표이미지
				//$rtn_data[$k]['X'] = $v_arr['CA'];	// 일련번호

				// 성공
				if($rtn_market_data_josn['info']['success'] === true)
					$rtn_data[$k]['Z'] = '성공';
				else
					$rtn_data[$k]['Z'] = '실패';

			}

			$rtn_data_json = json_encode($rtn_data);
			$rtn_data_json = str_replace('null', '', $rtn_data_json);
			echo $rtn_data_json;
		}

		delete_files($config['upload_path'], true);
	}

	// 엑셀 샘플 다운로드
	function excel_sample_down()
	{
		$user_id = $this->session->userdata('user_id');
		$sample_file = '/home/ubuntu/data/files/excel/product_sample_ver1.xls';

		if(file_exists($sample_file))
		{
			$this->load->helper('download');

			$down_file_name = 'AutoDaV1.0.'.time().'.xls';
			$sample_data = file_get_contents($sample_file);

			force_download($down_file_name, $sample_data);
		}
	}

	function goods_cate_update()
	{
		exit;
		if(isset($this->config_vars['open_market2']))
		{
			foreach($this->config_vars['open_market2'] AS $market => $v)
			{
				$query = $this->db->query("SELECT cate_json FROM market_goods_cate WHERE market='$market'");
				if($query->num_rows() > 0)
				{
					$row = $query->row();
					//debug($row);
					$rtn_json = $row->cate_json;
					$rtn_arr = json_decode($rtn_json, 1);
					//debug($rtn_arr);

					$insert_data = array();
					foreach($rtn_arr AS $k => $v)
					{
						$insert_data[$k] = array(
                            'market' => $market,
                            'LargeCategory'  => $v['LargeCategory']['Name'],
                            'MiddleCategory' => $v['MiddleCategory']['Name'],
                            'SmallCategory'  => $v['SmallCategory']['Name'],
                            'DetailCategory' => $v['DetailCategory']['Name'],
                            'CategoryCode'	 => $v['CategoryCode']['Code'],
                            'created'		 => $this->info['created'],
						);
					}

					$this->db->insert_batch('goods_cate', $insert_data);
				}
			}
		}
	}

	function get_goods_cate()
	{
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		$this->view_data['link_tag3'] = link_tag('/include/plugins/datepicker/datepicker3.css');
		// $this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		// $this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		// $this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		// $this->view_data['link_tag4'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		// 카테고리 목록
		$this->view_data['cate1'] = $this->get_category();

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_cate_list', $this->view_data);
		$this->load->view('bottom');
	}

	// 카테고리 조회
	function ajax_goods_cate_list()
	{
		//debug($_GET);
		$user_id = $this->session->userdata('user_id');

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$where = "";

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;
				switch ($rule->op)
				{
					case "eq":
						$fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "") $whereArray[] = $fieldName.$fieldOperation;
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = "";
			//debug($where);
		}

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx)
		{
			$sidx = "LargeCode, MiddleCode, SmallCode";
		}

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 카테목록
		$query = $this->db->query("SELECT count(id) AS cnt FROM goods_cate WHERE market='H' {$where}");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

        // $sql = "
        //     SELECT
        //         GSC.id, GSC.market, GSC.LargeCategory, GSC.LargeCode, GSC.MiddleCategory, GSC.MiddleCode, GSC.SmallCategory, GSC.SmallCode, GSC.activated, GSC.created,
        //         COUNT(GSC.id) AS GoodsCnt
        //         FROM
        //             goods_cate  AS GSC LEFT OUTER JOIN
        //             goods	    AS GS ON GS.market='H' AND ( GS.Category1 = GSC.id OR GS.Category2 LIKE 'GSC.id%' OR GS.Category3 LIKE 'GSC.id%' )
        //         WHERE
        //             GSC.market='H'
        //             {$where}
        //         GROUP BY
        //             GSC.id
        //         ORDER BY
        //             {$sidx} {$sord }
        //         LIMIT {$start}, {$limit}
        // ";
        $sql = "
            SELECT
                GSC.id, GSC.market, GSC.LargeCategory, GSC.LargeCode, GSC.MiddleCategory, GSC.MiddleCode, GSC.SmallCategory, GSC.SmallCode, GSC.activated, GSC.modified
                FROM
                    goods_cate  AS GSC
                WHERE
                    GSC.market='H'
                    {$where}
                GROUP BY
                    GSC.id
                ORDER BY
                    {$sidx} {$sord }
                LIMIT {$start}, {$limit}
        ";
        // debug($sql);

		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
                // $GoodsCnt = 0;
                $GoodsCntQuery = $this->db->query("SELECT count(id) AS cnt FROM goods WHERE user_id=5 AND market='H' AND Category3 LIKE '{$row->id}%'");
                $GoodsCntRow = $GoodsCntQuery->row();
                $GoodsCnt = $GoodsCntRow->cnt;


				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
                    $row->id,
                    $this->config_vars['open_market2'][$row->market],
                    $GoodsCnt,
                    $row->LargeCategory,
                    $row->LargeCode,
                    $row->MiddleCategory,
                    $row->MiddleCode,
                    $row->SmallCategory,
                    $row->SmallCode,
                    $row->activated,
                    '',
                    $row->modified,
                );

				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 카테고리별 상품정보 조회
	function ajax_goods_cate_list_goods()
    {
		//debug($_GET);
		$CateId = $this->uri->segment(3);
		$user_id = $this->session->userdata('user_id');

		$filters = $this->input->post('filters');
		$search = $this->input->post('_search');

		$where = "";

		if(($search==true) &&($filters != ""))
		{
			$filters = json_decode($filters);
			$where = " and ";
			$whereArray = array();
			$rules = $filters->rules;
			$groupOperation = $filters->groupOp;

			foreach($rules as $rule)
			{
				$fieldName = $rule->field;
				$fieldData = $rule->data;
				switch ($rule->op)
				{
					case "eq":
						$fieldOperation = " = '".$fieldData."'";
						break;
					case "ne":
						$fieldOperation = " != '".$fieldData."'";
						break;
					case "lt":
						$fieldOperation = " < '".$fieldData."'";
						break;
					case "gt":
						$fieldOperation = " > '".$fieldData."'";
						break;
					case "le":
						$fieldOperation = " <= '".$fieldData."'";
						break;
					case "ge":
						$fieldOperation = " >= '".$fieldData."'";
						break;
					case "nu":
						$fieldOperation = " = ''";
						break;
					case "nn":
						$fieldOperation = " != ''";
						break;
					case "in":
						$fieldOperation = " IN (".$fieldData.")";
						break;
					case "ni":
						$fieldOperation = " NOT IN '".$fieldData."'";
						break;
					case "bw":
						$fieldOperation = " LIKE '".$fieldData."%'";
						break;
					case "bn":
						$fieldOperation = " NOT LIKE '".$fieldData."%'";
						break;
					case "ew":
						$fieldOperation = " LIKE '%".$fieldData."'";
						break;
					case "en":
						$fieldOperation = " NOT LIKE '%".$fieldData."'";
						break;
					case "cn":
						$fieldOperation = " LIKE '%".$fieldData."%'";
						break;
					case "nc":
						$fieldOperation = " NOT LIKE '%".$fieldData."%'";
						break;
					default:
						$fieldOperation = "";
						break;
				}

				if($fieldOperation != "") $whereArray[] = $fieldName.$fieldOperation;
			}

			if (count($whereArray)>0)
				$where .= join(" ".$groupOperation." ", $whereArray);
			else
				$where = "";
			//debug($where);
		}

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx)
		{
			$sidx = "created";
		}

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 카테목록
		$query = $this->db->query("SELECT count(id) AS cnt FROM goods WHERE user_id='5' AND market='H' AND Category3 LIKE '{$CateId}%' {$where}");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$query = $this->db->query("SELECT id, user_id, market, Category1, Category2, Category3, GoodsName, GoodsPrice, GoodsCode, GoodsImage, activated, created FROM goods WHERE user_id='5' AND market='H' AND Category3 LIKE '{$CateId}%' ORDER BY {$sidx} {$sord} LIMIT {$start}, {$limit}");

		//echo "SELECT id, user_id, market, Category1, Category2, Category3, GoodsName, GoodsPrice, GoodsCode, GoodsImage, activated, created FROM goods WHERE user_id='5' AND market='H' AND Category3 LIKE '{$CateId}%' ORDER BY {$sidx} {$sord} LIMIT {$start}, {$limit}";

		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
                $Category2Arr = explode('|', $row->Category2);
                $Category3Arr = explode('|', $row->Category3);

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
                    $row->id,
                    '',
                    $row->GoodsName,
                    $row->GoodsCode,
                    $row->Category1,
                    $Category2Arr[0],
                    $Category3Arr[0],
                    $row->activated,
                    $row->created,
                    //$row->activated
                );
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 카테고리별 정보
	function get_goods_cate_one()
	{
		$CateId = $this->input->post('cateId');

		if(!$CateId) {
			echo '{"info":{"success":false,"text":"정상적인 접근이 아닙니다."}}';
            exit;
        }

        // 변경할 카테고리가 있는지 확인
		$query = $this->db->query("SELECT * FROM goods_cate WHERE id='{$CateId}'");
		$row = $query->row();
        // debug($row);

		echo json_encode($row);
	}

	// 카테고리별 정보 개별 수정
	function set_goods_cate()
	{
		$Gb = $this->input->post('Gb');
		$GoodsCateId = $this->input->post('GoodsCateId');

		$LargeCategory = $this->input->post('LargeCategory');	// 대분류
		$LargeCode = $this->input->post('LargeCode');	// 대코드
		$MiddleCategory = $this->input->post('MiddleCategory'); // 중분류
		$MiddleCode = $this->input->post('MiddleCode'); // 중코드
		$SmallCategory = $this->input->post('SmallCategory'); 	// 소분류
		$SmallCode = $this->input->post('SmallCode'); 	// 중코드
		$activated = $this->input->post('activated'); 	// 상태

		if(!$Gb) {
			echo '{"info":{"success":false,"text":"정상적인 접근(1)이 아닙니다."}}';
            exit;
        }

        // 카테고리 테이블 수정
        $cate_data = array(
            'LargeCategory'	=> $LargeCategory,
            'LargeCode'	=> $LargeCode,
            'MiddleCategory'	=> $MiddleCategory,
            'MiddleCode'	=> $MiddleCode,
            'SmallCategory'	=> $SmallCategory,
            'SmallCode'	=> $SmallCode,
            'activated'	=> $activated,
            'modified'	=> $this->info['created'],
        );

        // 수정
        if($Gb == 'E')
        {
            if(!$GoodsCateId) {
                echo '{"info":{"success":false,"text":"정상적인 접근(2)이 아닙니다."}}';
                exit;
            }

            $where_array = array('id' => $GoodsCateId);
            $this->db->where($where_array);
            $this->db->update('goods_cate', $cate_data);
        }
        // 복사
        else if($Gb == 'C')
        {
            $cate_data['market'] = 'H';
            $cate_data['created'] = $this->info['created'];
		    $this->db->insert('goods_cate', $cate_data);
        }
        else {
            echo '{"info":{"success":false,"text":"정상적인 접근(3)이 아닙니다."}}';
            exit;
        }

		echo '{"info":{"success":true}}';
	}

    // 카테고리 정보 수정
	function ajax_goods_cate_edit()
	{
        // debug($this->input->post());exit;
		$oper = $this->input->post('oper');

		$LargeCategory = $this->input->post('LargeCategory');	// 대분류
		$LargeCode = $this->input->post('LargeCode');	// 대코드
		$MiddleCategory = $this->input->post('MiddleCategory'); // 중분류
		$MiddleCode = $this->input->post('MiddleCode'); // 중코드
		$SmallCategory = $this->input->post('SmallCategory'); 	// 소분류
		$SmallCode = $this->input->post('SmallCode'); 	// 중코드

        $CateId = $this->input->post('CateId');

        if($oper != 'edit')
        {
            echo '{"error": "정상적인 접근이 아닙니다!"}';
            return;
        }

		if($LargeCategory)
		{
			// 카테고리 테이블 수정
			$cate_data = array(
				'LargeCategory'	=> $LargeCategory
			);
		}

		if($LargeCode > 0)
		{
			// 카테고리 테이블 수정
			$cate_data = array(
				'LargeCode'	=> $LargeCode
			);
		}

		if($MiddleCategory)
		{
			// 카테고리 테이블 수정
			$cate_data = array(
				'MiddleCategory'	=> $MiddleCategory
			);
		}

		if($MiddleCode > 0)
		{
			// 카테고리 테이블 수정
			$cate_data = array(
				'MiddleCode'	=> $MiddleCode
			);
		}

		if($SmallCategory)
		{
			// 카테고리 테이블 수정
			$cate_data = array(
				'SmallCategory'	=> $SmallCategory
			);
		}

		if($SmallCode > 0)
		{
			// 카테고리 테이블 수정
			$cate_data = array(
				'SmallCode'	=> $SmallCode
			);
		}

        //debug($goods_data);
        $where_array = array('id' => $CateId);
        $this->db->where($where_array);
        $this->db->update('goods_cate', $cate_data);

        echo '{"error": ""}';
    }

	// 카테고리별 상품등록수 개별카테반영
	function ajax_goods_cate_list_goods_change()
	{
		$GoodsId = $this->input->post('goodsId');
		$Category1 = $this->input->post('category1');
		$Category2 = $this->input->post('category2');
		$Category3 = $this->input->post('category3');

		if(!$GoodsId && !$Category1 && !$Category2 && !$Category3) {
			echo '{"info":{"success":false,"text":"정상적인 접근이 아닙니다."}}';
            exit;
        }

        // debug($GoodsId);
        // debug($Category1);
        // debug($Category2);
        // debug($Category3);
        // exit;

        // 변경할 카테고리가 반영할 상품 카테와 동일한지 확인
		$query = $this->db->query("SELECT count(id) AS cnt FROM goods WHERE id='{$GoodsId}' AND Category1 = '{$Category1}' AND Category2 LIKE '{$Category2}%' AND Category3 LIKE '{$Category3}%'");
		$row = $query->row();
		$existingCount = $row->cnt;

        if($existingCount > 0) {
			echo '{"info":{"success":false,"text":"선택한 카테고리는 해당 상품에 이미 등록되어 있어요."}}';
            exit;
        }

        // 해당 상품에 변경할 카테고리 반영
		$goods_data = array(
			'Category1'	=> $Category1,
			'Category2'	=> $Category2.'|1',
			'Category3'	=> $Category3.'|2'
		);
		// debug($goods_data);exit;
		$this->db->where('id', $GoodsId);
		$this->db->update('goods', $goods_data);

		echo '{"info":{"success":true,"text":"반영되었습니다!"}}';
	}

	// 상품 자동 전송 관리
	function goods_cron()
	{

		$this->view_data['config'] = $this->config_vars;

		$this->load->helper('html');

		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');
		$this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag4'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');

		// 마켓목록
		$query = $this->db->query("SELECT market FROM user_market WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['markets'] = $query->result();

		// 전체상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['goods_all_cnt'] = $query->num_rows();

		// 선택상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' AND SendCheck='Y' ORDER BY id DESC");
		$this->view_data['goods_chk_cnt'] = $query->num_rows();
		//debug($this->view_data);

		// 일일 전송 상품수
		$query = $this->db->query("SELECT id FROM goods_cron WHERE user_id='{$this->view_data['user_id']}' AND SUBSTRING(created,1,10)='".date("Y-m-d", time())."' ORDER BY id DESC");
		$this->view_data['goods_day_cnt'] = $query->num_rows();

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_cron', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품 조회
	function cron_master_ajax_list()
	{
		//debug($_GET);
		$user_id = $this->session->userdata('user_id');

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "created";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		// 상품목록
		$query = $this->db->query("SELECT count(id) AS cnt FROM goods_cron WHERE user_id='{$user_id}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$sql = "SELECT
					GS.*,
					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4
					FROM
						goods_cron			AS GS LEFT OUTER JOIN
						goods_image_cron	As GSI ON GS.id = GSI.goods_id
					WHERE
						user_id='{$user_id}'
					ORDER BY
						$sidx $sord
					LIMIT
						$start , $limit
		";
		//$query = $this->db->query("SELECT id, user_id, GoodsName, GoodsPrice, GoodsCount, GoodsImage, created FROM goods WHERE user_id='{$user_id}' ORDER BY $sidx $sord LIMIT $start , $limit");
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				$goods_delete_info = '';

				/*
				switch($row->market)
				{
					// 신상마켓
					case "G":
						$goods_delete_info = $this->sinsang_scrap->goods_delete_info($row->market, $row->GoodsNo);
						break;
				}

				if($goods_delete_info) $delete = 'N';
				else $delete = 'Y';
				*/

				if($row->activated == 1) $send = '수동';
				if($row->activated == 2) $send = '자동';

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													$this->config_vars['open_market2'][$row->market],
													$send,
													$row->DelGb,
													$row->img0,
													$row->GoodsNo,
													$row->GoodsName,
													$row->GoodsPrice,
													$row->img1,
													$row->img2,
													$row->img3,
													$row->img4,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 상품 조회
	function cron_ajax_list()
	{
		//debug($_GET);
		$GdsMstId = $this->uri->segment(3);
		$user_id = $this->session->userdata('user_id');

		$query = $this->db->query("SELECT id, user_id, GdsMstId, market, GoodsName, GoodsPrice, GoodsCount, GoodsImage, GoodsNo, activated, created FROM goods WHERE user_id='{$user_id}' AND GdsMstId='{$GdsMstId}' ORDER BY id ASC");

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				// 11번가는 상품 이미지 조회 후 저장
				if($row->market == 'C')
				{
					$pos = strpos($row->GoodsImage, 'image.11st.co.kr');
					// 업데이트 된 이미지가 아닐 때 조회 후 저장
					if($pos === false && $row->GoodsNo)
					{
						$rtn_img = $this->elevenst_scrap->goods_img_process($row->id, $row->GoodsNo); // 상품번호로 조회
						if($rtn_img) $row->GoodsImage = $rtn_img;
					}
				}

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													'',
													$row->GoodsNo,
													$row->GoodsImage,
													$this->config_vars['open_market2'][$row->market],
													$row->GoodsName,
													$row->GoodsPrice,
													$row->GoodsCount,
													$row->created,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

	// 리얼 상품 선택 삭제
	function goods_cron_select_erase()
	{
		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($goodsId);exit;

		$deleting_cnt = count($goodsId); // 삭제할 상품수
		$deletede_cnt = 0; // 삭제된 상품수

		foreach($goodsId AS $k => $goods_id)
		{
			if($goods_id > 0)
			{
				$rtn_json = $this->goods_cron_erase($goods_id, '1');
				$rtn_arr = json_decode($rtn_json, 1);
				if($rtn_arr['info']['success'])
					$deletede_cnt++; // 삭제 성공 시 카운트
			}
		}

		echo '{"info":{"success":true,"text":"선택상품 ['.$deleting_cnt.' 개] 중 ['.$deletede_cnt.' 개] 상품이 삭제 되었습니다."}}';
	}

	// 상품개별삭제
	function goods_cron_erase($goods_id='', $rtnView='')
	{
		$user_id = $this->session->userdata('user_id');

		if(!$goods_id)
			$goods_id = $this->input->post('goodsId');

		//$goods_img_path = '/home/autoda/data/files/goods/'.$user_id.'/'.$goods_id.'/';

		if($goods_id > 0)
		{
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.Description, GSD.NoticeItemCodes,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods_cron AS		GS LEFT OUTER JOIN
							goods_detail_cron	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image_cron	As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$goods_id}'
			";
			//debug($sql);
			$query = $this->db->query($sql);
			$row = $query->row();

			// 회원 상품이 맞는지 확인
			if($row->user_id == $user_id)
			{
				// 상품번호가 존재하면 해당 마켓별 상품 삭제 후 디비 삭제
				if($row->GoodsNo != '0')
				{
					//debug($row->market);exit;
					$rtn = $this->market_delete($row->market, trim($row->GoodsNo));
					//debug('rtn : '.$rtn);
				}

				if(isset($rtn) && $rtn)
				{
					$rtn_arr = json_decode($rtn, 1);
					if($rtn_arr['result'] == 'success')
					{
						$update_data = array(
									   'DelGb'	=> 'Y'
									);
						$this->db->where('id', $row->id);
						$this->db->update('goods_cron', $update_data);

						if($rtnView)
							return '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
						else
							echo '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
					}
					else
					{
						if($rtnView)
							return '{"info":{"success":false,"text":"처리 오류(2)입니다."}}';
						else
						{
							echo '{"info":{"success":false,"text":"처리 오류(2)입니다."}}';
							exit;
						}
					}
				}
				else
				{
					$update_data = array(
								   'DelGb'	=> 'Y'
								);
					$this->db->where('id', $row->id);
					$this->db->update('goods_cron', $update_data);

					if($rtnView)
						return '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
					else
						echo '{"info":{"success":true,"text":"상품삭제 완료입니다."}}';
				}
			}
			else
			{
				if($rtnView)
					return '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
				else
				{
					echo '{"info":{"success":false,"text":"처리 오류(1)입니다."}}';
					exit;
				}
			}
		}
	}

	// 리얼 상품 정보 조회
	function goods_cron_info()
	{
		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');
		//debug($goodsId);exit;

		if($goodsId > 0)
		{
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.*,
						GSD.Description, GSD.NoticeItemCodes,
						GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
						FROM
							goods_cron AS		GS LEFT OUTER JOIN
							goods_detail_cron	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
							goods_image_cron	As GSI ON GS.id = GSI.goods_id
						WHERE GS.id='{$goodsId}'
			";
			//debug($sql);
			$query = $this->db->query($sql);
			$row = $query->row();
			//debug($row);

			// 회원 상품이 맞는지 확인
			if($row->user_id == $user_id)
			{
				// 상품번호가 존재하면 해당 마켓별 상품 삭제 후 디비 삭제
				if($row->GoodsNo != '0')
				{
					$rtn = '';
					switch($row->market)
					{
						// 신상마켓
						case "G":
							$rtn = $this->sinsang_scrap->goods_delete_info($row->market, $row->GoodsNo);
							break;
					}

					if(!$rtn)
					{
						$update_data = array(
									   'DelGb'	=> 'Y'
									);
						$this->db->where('id', $row->id);
						$this->db->update('goods_cron', $update_data);

						echo '{"info":{"success":false,"text":"리얼 마켓에서 삭제된 상품입니다."}}';
					}
					else
						echo '{"info":{"success":true,"text":"리얼 마켓에서 존재하는 상품입니다."}}';
				}
			}
		}
	}

	function goods_error_log()
	{
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/extensions/Scroller/css/dataTables.scroller.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/examples/resources/syntax/shCore.css');

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_cron_error_list.php', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품 조회
	function goods_cron_error_log_ajax_list()
	{
		$v1_code_arr = $this->config->item('v1_code');
		//debug($v1_code_arr);

		//debug($_GET);
		$user_id = $this->session->userdata('user_id');

		$page = $_REQUEST['page']; // get the requested page
		$limit = $_REQUEST['rows']; // get how many rows we want to have into the grid
		$sidx = $_REQUEST['sidx']; // get index row - i.e. user click to sort
		$sord = $_REQUEST['sord']; // get the direction
		if(!$sidx) $sidx = "VS.regdate";

		$totalrows = isset($_REQUEST['totalrows']) ? $_REQUEST['totalrows']: false;
		if($totalrows) {
			$limit = $totalrows;
		}

		$common_from = '
			FROM
				v1_status	AS VS JOIN
				goods		AS GS ON VS.goods_id = GS.id JOIN
				goods_image	As GSI ON GS.id = GSI.goods_id
		';

		// 상품 전송 로그 목록
		$query = $this->db->query("SELECT count(VS.id) AS cnt {$common_from} WHERE VS.user_id='{$user_id}'");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if( $count >0 ) {
			$total_pages = ceil($count/$limit);
		} else {
			$total_pages = 0;
		}
		if ($page > $total_pages) $page=$total_pages;
		if ($limit<0) $limit = 0;
		$start = $limit*$page - $limit; // do not put $limit*($page - 1)
		if ($start<0) $start = 0;

		$responce = new stdClass();

		$sql = "SELECT
					VS.*,
					GS.*,
					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4
					{$common_from}
					WHERE
						VS.user_id='{$user_id}'
					ORDER BY
						$sidx $sord
					LIMIT
						$start , $limit
		";
		//debug($sql);
		$query = $this->db->query($sql);
		$responce->records = $count;
		$responce->page = $page;
		$responce->total = $total_pages;

		if($query->num_rows() > 0)
		{
			$i=0;
			foreach ($query->result() as $row)
			{
				//debug(count(explode('/', $row->GoodsImage)));
				if(count(explode('/', $row->GoodsImage)) > 1)
					$GoodsImage	= $row->GoodsImage;
				else
				{
					$GoodsImage	 = '/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->GoodsImage;
					$GoodsImage1 = ($row->img1)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img1:'';
					$GoodsImage2 = ($row->img2)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img2:'';
					$GoodsImage3 = ($row->img3)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img3:'';
					$GoodsImage4 = ($row->img4)?'/data/files/goods/'.$row->user_id.'/thumbnail/'.$row->img4:'';
				}

				//if($row->SendCheck == 'Y') $SendCheck = '';

				$StInfo = json_decode($row->StInfo, 1);
				//$StInfo['resultMsg'] = $row->StInfo;
				if(!$StInfo) $StInfo['resultMsg'] = $row->StInfo;

				$responce->rows[$i]['id'] = $row->id;
				$responce->rows[$i]['cell'] = array(
													$row->id,
													$row->GdsMstId,
													$row->SendCheck,
													$row->SendCount,
													$GoodsImage,
													$row->GoodsName,
													$row->StCode,
													$v1_code_arr[$row->StCode],
													$StInfo['resultMsg'],
													$row->regdate,
													//$row->activated
												);
				$i++;
			}
		}

		echo json_encode($responce);
	}

    // 알림톡 전송 테스트
	function aligo_send_test()
	{
		$ret = $this->aligo_sms->send_test();
        echo $ret;
    }

	// 상품 자동 전송 관리 테스트
	function goods_cron_test()
	{

		$this->view_data['config'] = $this->config_vars;

		$this->load->helper('html');

		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');
		$this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag4'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');

		// 마켓목록
		$query = $this->db->query("SELECT market FROM user_market WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['markets'] = $query->result();

		// 전체상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' ORDER BY id DESC");
		$this->view_data['goods_all_cnt'] = $query->num_rows();

		// 선택상품수
		$query = $this->db->query("SELECT id FROM goods WHERE user_id='{$this->view_data['user_id']}' AND SendCheck='Y' ORDER BY id DESC");
		$this->view_data['goods_chk_cnt'] = $query->num_rows();
		//debug($this->view_data);

		// 일일 전송 상품수
		$query = $this->db->query("SELECT id FROM goods_cron WHERE user_id='{$this->view_data['user_id']}' AND SUBSTRING(created,1,10)='".date("Y-m-d", time())."' ORDER BY id DESC");
		$this->view_data['goods_day_cnt'] = $query->num_rows();

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_cron_test', $this->view_data);
		$this->load->view('bottom');
	}

	function goods_test()
	{

		// $output = shell_exec("find /home/danharoo/www/data/files/goods/5 -type f 2> /dev/null | wc -l");
		// $output = shell_exec("touch -t 201702010000 201702_start.txt | touch -t 201702282359 201702_end.txt | ls");
		// $output = shell_exec("find /home/danharoo/www/data/files/goods/5 -type f -newer 201702_start.txt -a ! -newer 201702_end.txt");
		// $output = shell_exec("touch -t 201703010000 201703_start.txt | touch -t 201703312359 201703_end.txt");
		// $output = shell_exec("find /home/danharoo/www/data/files/goods/5 -type f -newer 201703_start.txt -a ! -newer 201703_end.txt");
		// shell_exec("touch -t 201704010000 201704_start.txt | touch -t 201704302359 201704_end.txt");

		// shell_exec("touch -t 201705010000 201705_start.txt | touch -t 201705312359 201705_end.txt");
		// $output = shell_exec("find /home/danharoo/www/data/files/goods/5 -type f -newer 201705_start.txt -a ! -newer 201705_end.txt");

		// debug(explode('\r', $output));
		// echo "<pre>$output</pre>";
		// $oparray = preg_split('/\s+/', trim($output));
		// debug($oparray);

		// $this->load->view('top', $this->view_data);
		// $this->load->view('products/goods_image_status', $this->view_data);
		// $this->load->view('bottom');
	}

	// 판매처 FTP정보 관리
	function ftp_manager()
	{
		$this->load->library('pagination');

		$user_id = $this->session->userdata('user_id');

		$post_val = $this->input->post();
		if(is_array($post_val))
		{
			//debug($post_val);

			$insert_data = array(
						'ftp_name'		=> trim($post_val['ftp_name']),
						'ftp_host'		=> trim($post_val['ftp_host']),
						'ftp_id'		=> trim($post_val['ftp_id']),
						'ftp_pw'		=> trim($post_val['ftp_pw']),
						'ftp_folder'	=> trim($post_val['ftp_folder']),
						'created'		=> $this->info['created']
					);
			$this->db->insert('store_ftp_config', $insert_data);
			$rtn = $this->db->insert_id();

			if($rtn > 0)
				echo '{"info":{"success":true,"text":"완료입니다."}}';
			else
				echo '{"info":{"success":false,"text":"오류입니다."}}';

			return;
		}

		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/bootstrapvalidator/bootstrapValidator.min.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag3'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		$this->view_data['link_tag4'] = link_tag('/include/bower_components/bootstrap-dialog/dist/css/bootstrap-dialog.min.css');

		$order_by = " ORDER BY id DESC ";

        if($this->uri->segment(3))
            $page = $this->uri->segment(3);
        else
            $page = 1;

        $limit = 10;

		// FTP 관리 목록
		$query = $this->db->query("SELECT count(id) AS cnt FROM store_ftp_config WHERE 1");
		$row = $query->row();
		//$this->view_data['goods'] = $query->result();

		$count = $row->cnt;

		if(!$limit)
		{
			$limit_sql = '';
			$limit = $count;
		}
		else
		{
			if($page)
			{
				$limit_sql = "LIMIT ".($page-1)*$limit.", ".$limit;
				$order_by = " ORDER BY id DESC ";
			}
			else {
				$limit_sql = "LIMIT 0, ".$limit;
			}
		}

        $this->view_data['IconsDataTotal'] = $count;
        $this->view_data['SearchText'] = $this->input->get('search_text');

        $config['base_url'] = '/products/ftp_manager/';
		//$config['suffix'] = '?'.http_build_query($this->input->get(), '', "&amp;");
        $config['total_rows'] = $count;
        $config['uri_segment'] = 3;

        //$config['cur_tag_open'] = '<li class="active"><a href="javascript://">';
		$config['first_url'] = $config['base_url'] . $config['suffix'];

        $config['per_page'] = $limit;
        $config['num_links'] = 3;
        $config['use_page_numbers'] = TRUE;
		$config['reuse_query_string'] = TRUE;
        //$config['page_query_string'] = TRUE;
        //debug($config);
        $this->pagination->initialize($config);
        $this->view_data['GroupPages'] = $this->pagination;
        //debug($this->pagination);
        //echo $this->pagination->create_links();

		// FTP 관리 목록
		$query = $this->db->query("SELECT * FROM store_ftp_config WHERE 1 {$order_by} {$limit_sql}");
		$this->view_data['list'] = $query->result();
		//debug($this->view_data);

		$this->load->view('top', $this->view_data);
		$this->load->view('products/ftp_manager', $this->view_data);
		$this->load->view('bottom');
	}

    // FTP 접속 확인
	function ftp_access_check()
	{
        $this->load->library('ftp');

		// $clickDir1 = $this->uri->segment(3);
		// $clickDir2 = $this->uri->segment(4);

		$post_val = $this->input->post();

        if($post_val['fid'] < 1) {
			echo '{"info":{"success":false,"text":"정상적인 접근(1)이 아닙니다."}}';
            exit;
        }

		// FTP 관리 목록
		$query = $this->db->query("SELECT * FROM store_ftp_config WHERE id={$post_val['fid']}");
		$row = $query->row();

        if(!$row->id) {
			echo '{"info":{"success":false,"text":"정상적인 접근(2)이 아닙니다."}}';
            exit;
        }

        $config['hostname'] = $row->ftp_host;
        $config['username'] = $row->ftp_id;
        $config['password'] = $row->ftp_pw;
        $config['debug'] = TRUE;

        if($this->ftp->connect($config)) {
			echo '{"info":{"success":true,"text":"접속 성공!"}}';
        }
        else
			echo '{"info":{"success":false,"text":"접속할 수 없습니다."}}';

        // $this->ftp->close();
        // exit;
        // Creates a folder named "bar"
        // if($this->ftp->mkdir('/img/acc244k19/', DIR_WRITE_MODE))
        // {
        //     $copyPathDir = '/home/danharoo/www/data/files/goods/goodscode/img/acc244k19/';

        //     $this->ftp->mirror($copyPathDir, '/img/acc244k19/');
        // }

        // $dir = '/';
        // if($clickDir1) $dir .= $clickDir1;
        // if($clickDir2) $dir .= '/'.$clickDir2;

        // $list = $this->ftp->list_files($dir);

        // // debug($list);

        // echo 'FTP 루트 목록<br />';

        // foreach ($list as $key => $value)
        // {
        //     if($clickDir1)
        //         echo $value.'<br />';
        //     else
        //         echo '<a href="/products/ftp_access_check'.$value.'">'.$value.'</a><br />';
        // }

        $this->ftp->close();

		exit;

	}

	// FTP 관리 전체 저장
	function ftp_setting_all()
	{
		$post_val = $this->input->post();
		if(is_array($post_val))
		{
			//debug($post_val);exit;

			if(is_array($post_val['id']))
			{
				foreach($post_val['id'] AS $k => $v)
				{
					$update_data[$k] = array(
                        'id'			=> $v,
                        'ftp_name'		=> trim($post_val['ftp_name'][$k]),
                        'ftp_host'		=> trim($post_val['ftp_host'][$k]),
                        'ftp_id'		=> trim($post_val['ftp_id'][$k]),
                        'ftp_pw'		=> trim($post_val['ftp_pw'][$k]),
                        'ftp_folder'	=> trim($post_val['ftp_folder'][$k]),
                        'modified'		=> $this->info['created']
					);
				}
			}

			$this->db->update_batch('store_ftp_config', $update_data, 'id');
		}

        echo '{"info":{"success":true,"text":"완료입니다."}}';
        exit;
	}

	// 상품 사이즈표 목록관리, 2022.08.24
	function goods_size_manage() {
		$this->load->model('goods_m');

		$this->view_data['list'] = $this->goods_m->get_goods_size_graph();	

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_size_manage', $this->view_data);
		$this->load->view('bottom');
	}

	// 상품 정보 목록관리, 2022.09.08
	function goods_info_manage() {
		$this->load->model('goods_m');

		$this->view_data['model_list'] = $this->goods_m->get_goods_model_info_manage();
		$this->view_data['color_list'] = $this->goods_m->get_goods_color_list();
		$this->view_data['fit_list'] = $this->goods_m->get_goods_fit_list();
		$this->view_data['option_etc_list'] = $this->goods_m->get_goods_option_etc();

		$this->load->view('top', $this->view_data);
		$this->load->view('products/goods_info_manage', $this->view_data);
		$this->load->view('bottom');
	}

	function modify_goods_model_useing() {
		$gmi_no = isset($_REQUEST['no']) ? $_REQUEST['no']:'';
		$old_use_yn = isset($_REQUEST['use_yn']) ? $_REQUEST['use_yn']:'';

		if($gmi_no == '' || $old_use_yn == '') {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$gmi_use_yn = "";
		$return_flag = null;
		if($old_use_yn == 'Y') {
			$gmi_use_yn = 'N';
		}else {
			$gmi_use_yn = 'Y';
		}

		$this->load->model('goods_m');
		$result = $this->goods_m->update_goods_model_use_yn($gmi_no, $old_use_yn, $gmi_use_yn, $this->view_data['user_id']);

		if($result) {
			echo '{"info":{"success":"true","text":"수정완료"}}';
		}else {
			echo '{"info":{"success":"false","text":"수정오류"}}';
		}
	}

	function modify_model_data() {
		$gmi_no = isset($_REQUEST['no']) ? $_REQUEST['no']:'';
		$change_data = isset($_REQUEST['change_data']) ? $_REQUEST['change_data']:'';

		if($gmi_no == '' || $change_data == '') {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$data = json_decode($change_data, true);

		$this->load->model('goods_m');
		$result = $this->goods_m->update_goods_model_graph($gmi_no, $data, $this->view_data['user_id']);
	
		if($result) {
			echo '{"info":{"success":"true","text":"수정완료"}}';
		}else {
			echo '{"info":{"success":"false","text":"수정오류"}}';
		}
	}

	function modify_size_data() {
		$gsi_no = isset($_REQUEST['no']) ? $_REQUEST['no']:'';
		$set_type = isset($_REQUEST['set']) ? $_REQUEST['set']:'';
		$change_data = isset($_REQUEST['change_data']) ? $_REQUEST['change_data']:'';

		if($gsi_no == '' || $set_type == '' || $change_data == '') {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$data = json_decode($change_data, true);

		$this->load->model('goods_m');

		$result = null;
		if($set_type == 1) {
			$result = $this->goods_m->update_goods_size_graph($gsi_no, $data, $this->view_data['user_id']);
		}else if($set_type == 2) {
			$result = $this->goods_m->update_goods_set_size_graph($gsi_no, $data, $this->view_data['user_id']);
		}

		if($result) {
			echo '{"info":{"success":"true","text":"수정완료"}}';
		}else {
			echo '{"info":{"success":"false","text":"수정오류"}}';
		}
	}

	function modify_goods_size_useing() {
		$gsi_no = isset($_REQUEST['no']) ? $_REQUEST['no']:'';
		$old_use_yn = isset($_REQUEST['use_yn']) ? $_REQUEST['use_yn']:'';

		if($gsi_no == '' || $old_use_yn == '') {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$gsi_use_yn = "";
		$return_flag = null;
		if($old_use_yn == 'Y') {
			$gsi_use_yn = 'N';
		}else {
			$gsi_use_yn = 'Y';
		}

		$this->load->model('goods_m');
		$result = $this->goods_m->update_goods_size_use_yn($gsi_no, $old_use_yn, $gsi_use_yn, $this->view_data['user_id']);
	
		if($result) {
			echo '{"info":{"success":"true","text":"수정완료"}}';
		}else {
			echo '{"info":{"success":"false","text":"수정오류"}}';
		}
	}

	// 선택 상품 html 다운로드
	function goods_select_html_make() {
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$user_id = $this->session->userdata('user_id');
		$goodsId = $this->input->post('goodsId');

		// 상품관리자 추가
		if($this->view_data['auth_code'] == 12) {
			$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
		}

		if(!$goodsId) {
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}

		$setting_cnt = count($goodsId); // 처리할 상품수
		$success_cnt = 0; // 처리된 상품수

		foreach($goodsId AS $k => $goods_id) {
			if($goods_id > 0) {
				// 회원 상품이 맞는지 확인
				$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND id='{$goods_id}' ";
				$query1 = $this->db->query($sql1);

				if($query1->num_rows() > 0) {
					// 등록 상품 정보 확인
					$sql2 = "SELECT
								GS.DanharooGoodsName, GS.GoodsCode, GoodsCode_6, GoodsName,
								GSD.DanharooDescription
								FROM
									goods 			AS GS LEFT OUTER JOIN
									goods_detail	As GSD ON GS.id = GSD.goods_id 
								WHERE GS.GdsMstId='{$goods_id}'
					";
					$query2 = $this->db->query($sql2);
					$master_goods_cnt = $query2->num_rows();
					if($master_goods_cnt > 0) {
						$goods_info = $query2->row();

						$data_txt = '';
						if($goods_info->DanharooDescription) {
							$goods_code_data = str_replace(strtoupper($goods_info->GoodsCode), strtoupper($goods_info->GoodsCode.$goods_info->GoodsCode_6), $goods_info->GoodsName);

							$data_txt .= str_replace('<b><font size="3.5">[ '.$goods_info->DanharooGoodsName.' ]</font></b>', '<b><font size="3.5">[ '.$goods_code_data.' ]</font></b>', $goods_info->DanharooDescription);

							$data = array(
								$goods_info->GoodsCode.'.html' => $data_txt
							);
							
							$this->zip->add_data($data);
						}

						$success_cnt++;
					} else {
						echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
						exit;
					}
				} else {
					echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
					exit;
				}
			}
		}

		$this->zip->archive($this->config->item('user_temp_html_dir').'/goods_html_files.zip');
	
		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
	}

	// 상품 선택 html 압축 다운
	function goods_select_html_zip_down() {
		$this->load->library('zip'); // Zip 압축 클래스 초기화

		$this->zip->read_file($this->config->item('user_temp_html_dir').'/goods_html_files.zip');

		$this->zip->download('goods_html_'.date('YmdHis', time()).'.zip');
	}

	// 상품 html 파일 다운 excel 업로드 
	function upload_excel_file_for_html() {
		if(isset($_FILES['upload_excel_file'])) {
			if ($_FILES['upload_excel_file']['name'] && $_FILES['upload_excel_file']['size'] > 0) {
				$user_id = $this->session->userdata('user_id');

				if($this->view_data['auth_code'] == 12) {
					$user_id = 5;	// 단하루 고정 회원 일련번호 픽스
				}

				$config['upload_path'] = '/home/danharoo/www/data/files/excel/'.$user_id.'/';
			
				if(!is_dir($config['upload_path'])) mkdir($config['upload_path'], 0755, TRUE);
				$config['allowed_types'] = '*';
				$this->load->library('upload', $config);

				if( ! $this->upload->do_upload('upload_excel_file')) {
					$error = array('error' => $this->upload->display_errors());
					//debug($error);
					$rtn_data_json = json_encode($error);
					echo $rtn_data_json;
				}else {
					$this->load->library('zip'); // Zip 압축 클래스 초기화

					$data = array('upload_data' => $this->upload->data());

					$excel_data = array();

					$this->load->library('excel');
					$objPHPExcel = PHPExcel_IOFactory::load($data['upload_data']['full_path']);
					$sheetDataArr = $objPHPExcel->getActiveSheet()->toArray(null,true,true,true);
				
					foreach($sheetDataArr AS $k1 => $v1_arr) {
						if($k1 > 1) {
							if($v1_arr['A'] != null) {
								foreach($v1_arr AS $k3 => $v3) {
									if($v3 == null) $sheetDataArr[$k1][$k3] = '';
								}

								$excel_data[] = $sheetDataArr[$k1];
							}
						}
					}

					if(count($excel_data) < 1) {
						$error = array('error' => '엑셀 파일에 등록된 상품이 없거나 필수 항목이 누락된 상품입니다.');
						$rtn_data_json = json_encode($error);
						echo $rtn_data_json;
						exit;
					}

					$GoodsCode_arr = array();
					foreach($excel_data AS $k => $v_arr) {
						$v_arr_json = json_encode($v_arr);
						$v_arr = json_decode($v_arr_json, 1);

						array_push($GoodsCode_arr, $v_arr['A']);
					}

					$setting_cnt = count($GoodsCode_arr); // 처리할 상품수
					$success_cnt = 0; // 처리된 상품수

					foreach($GoodsCode_arr as $k => $GoodsCode) {
						if($GoodsCode) {
							$sql1 = " SELECT id FROM goods_master WHERE user_id='{$user_id}' AND GoodsCode='{$GoodsCode}' ";
							$query1 = $this->db->query($sql1);

							if($query1->num_rows() > 0) {
								// 등록 상품 정보 확인
								$sql2 = "SELECT
											GS.DanharooGoodsName, GS.GoodsCode, GoodsCode_6, GoodsName,
											GSD.DanharooDescription
											FROM
												goods 			AS GS LEFT OUTER JOIN
												goods_detail	As GSD ON GS.id = GSD.goods_id 
											WHERE GS.GoodsCode='{$GoodsCode}'
								";
								$query2 = $this->db->query($sql2);
								$master_goods_cnt = $query2->num_rows();
								if($master_goods_cnt > 0) {
									$goods_info = $query2->row();
			
									$html_data_txt = '';
									if($goods_info->DanharooDescription) {
										$goods_code_data = str_replace(strtoupper($goods_info->GoodsCode), strtoupper($goods_info->GoodsCode.$goods_info->GoodsCode_6), $goods_info->GoodsName);
			
										$html_data_txt .= str_replace('<b><font size="3.5">[ '.$goods_info->DanharooGoodsName.' ]</font></b>', '<b><font size="3.5">[ '.$goods_code_data.' ]</font></b>', $goods_info->DanharooDescription);
			
										$html_data_file = array(
											$goods_info->GoodsCode.'.html' => $html_data_txt
										);
										
										$this->zip->add_data($html_data_file);
									}
			
									$success_cnt++;
								} else {
									echo '{"info":{"success":false,"text":"회원님 상품이 없습니다."}}';
									exit;
								}
							} else {
								echo '{"info":{"success":false,"text":"회원님 상품이 아닙니다."}}';
								exit;
							}
						}
					}
					$this->zip->archive($this->config->item('user_temp_html_dir').'/goods_html_files.zip');

					delete_files($config['upload_path'], true);

					echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다."}}';
				}
			} else {
				echo '{"info":{"success":false,"text":"엑셀 파일에 등록된 상품이 없거나 필수 항목이 누락된 상품입니다."}}';
				exit;
			}
		} else {
			echo '{"info":{"success":false,"text":"잘못된 접근입니다."}}';
			exit;
		}
	}

	// 디지털 오션
	function digitaloceanApi(){
		require 'vendor/autoload.php';
		$response_url = "";
		$oceanFile = $this->input->post('oceanFile');
		$goodsCode = $this->input->post('goodsCode');
		// ★ 쿼리스트링 제거 (?v=xxx 등 캐시버스터)
		$oceanFile = strtok($oceanFile, '?');
		$i = 0;
		$j = 0;
		if($oceanFile != ''){
			// ★ 경로 정규화: 절대 URL이면 path만 추출
			if(preg_match('#^https?://#i', $oceanFile)) {
				$parsed = parse_url($oceanFile);
				$host = isset($parsed['host']) ? $parsed['host'] : '';
				if(strpos($host, 'digitaloceanspaces.com') !== false) {
					if(preg_match('#/img/(\d{6}/.+)$#', $parsed['path'], $m)) {
						$oceanFile = '/img/' . $m[1];
					} else {
						echo "001";
						return;
					}
				} else {
					$oceanFile = isset($parsed['path']) ? $parsed['path'] : $oceanFile;
				}
			}
			$oceanFile = str_replace('/thumbnail/', '/', $oceanFile);

			$d = "img/".date("Ym")."/";
			$fileName = $d.basename($oceanFile);
			$filePath = $_SERVER['DOCUMENT_ROOT'].$oceanFile;
			if(file_exists($filePath)){
				$source = fopen($filePath, 'rb');
				$client = \Aws\S3\S3Client::factory(array(
					'endpoint' => 'https://nyc3.digitaloceanspaces.com',
					'credentials' => array(
						'key'    => 'DO00UARV93J8NET7ARGQ',
						'secret' => '0cqWKKyqkq7Z1WwczMbvEPwIP2TSbbToTdT4a7vnJuo'
					),
					'region' => 'sgp1'
				));
				$result = $client->putObject([
					'Bucket'   => 'newtalk',
					'Key'      => $fileName,
					'Body'     => $source,
					'ACL'      => 'public-read',
					'Metadata' => array('x-amz-meta-my-key' => 'your-value')
				]);
				if($result['ObjectURL'] != ''){
					echo "200";
					$i = $i + 1;
				}else{
					echo "999";
					$j = $j + 1;
				}
				fclose($source);
			}else{
				$j = $j + 1;
				echo "000";
			}
		}
		$oceanPath = "https://newtalk.kr/data/files/goods/goodscode/img/".$goodsCode."/";
		$this->load->model('goods_m');
		$this->goods_m->get_goods_id_update($goodsCode, $oceanPath, $i, $j, $fileName);
	}

	function test2(){
			$path = "/home/danharoo/www/data/files/goods/goodscode/img/cd2860k39/";

			$arr = array();

			for($i=1;$i<=5;$i++){
				array_push($arr,$path."cd2860k39-s_".$i.".jpg");
			}

			$maxY = 0;
			$maxX = 0;
			// 전체 가변길이 구하기

			for($i=0; $i<count($arr); $i++){
				$data = @file_get_contents($arr[$i]);
				if ($data === false) {
					continue;
				}
				$im = @imagecreatefromstring($data);
				if (!$im) {
					continue;
				}
				$size = @getimagesize($arr[$i]);
				if ($size === false) {
					imagedestroy($im);
					continue;
				}
				// 높이값
				if($size[1] != 0){
					$maxY += $size[1];
				}
				// 너비값
				if($size[0] > $maxX){
					$maxX = $size[0];
				}
//echo 					$maxX."<br>";
				imagedestroy($im);
//				echo $maxY."<br>";
			}
			// 토탈 가변길이 이미지 생성
			$bg = imagecreatetruecolor  ( $maxX, $maxY);

			// 실제 이미지 합성
			$nowY = 0;
			for($j=0; $j<count($arr); $j++){
				$data = @file_get_contents($arr[$j]);
				if ($data === false) {
					continue;
				}
				$im = @imagecreatefromstring($data);
				if (!$im) {
					continue;
				}
				$size = @getimagesize($arr[$j]);
				if ($size === false) {
					imagedestroy($im);
					continue;
				}
				$mimeType = $size['mime'];

				if($size[1] != 0){
					if($mimeType == "image/jpeg" || $mimeType == "image/jpg"){
						$nextImage = @imagecreatefromjpeg($arr[$j]);
					}elseif($mimeType  == "image/gif"){
						$nextImage = @imagecreatefromgif($arr[$j]);
					}elseif($mimeType  == "image/png"){
						$nextImage = @imagecreatefrompng($arr[$j]);
					}else{
						imagedestroy($im);
						return 0;
						// 합성 불가
					}
					if (!$nextImage) {
						imagedestroy($im);
						continue;
					}
//						echo $nowY."<br>";
					imagecopymerge($bg, $nextImage, 0, $nowY, 0, 0, $maxX, $maxY, 100);
					$nowY += $size[1];
					imagedestroy($nextImage);
				}
				imagedestroy($im);
			}

//		header('Content-Type: '.$mimeType);
//		imagejpeg($bg);
//		imagejpeg($bg, $path."sample_test.jpg",100);
	}

	function test()
	{
        // echo parse_url('https://developers.cafe24.com/web/upload/NNEditor/20180130/12ecf27747401c8502ddd6b2e79e1e64.png', PHP_URL_PATH);

        // $dir = '/img/test/';

        // // set up basic connection
        // $ftp = ftp_connect('ddglindashop.cafe24.com');

        // // login with username and password
        // $login_result = ftp_login($ftp, 'ddglindashop', '!!ddg0415');

        // // try to create the directory $dir
        // if (ftp_mkdir($ftp, $dir)) {
        //     echo "successfully created $dir\n";
        // } else {
        //     echo "There was a problem while creating $dir\n";
        // }

        // // close the connection
        // ftp_close($ftp);

        // $this->load->library('ftp');

		// $clickDir1 = $this->uri->segment(3);
		// $clickDir2 = $this->uri->segment(4);

        // $config['hostname'] = 'ddglindashop.cafe24.com';
        // $config['username'] = 'ddglindashop';
        // $config['password'] = '!!ddg0415';
        // $config['debug'] = FALSE;

        // if($this->ftp->connect($config)) {
		// 	echo '{"info":{"success":true,"text":"접속 성공!"}}';
        // }
        // else
		// 	echo '{"info":{"success":false,"text":"접속할 수 없습니다."}}';

        // $this->ftp->connect($config);

        // // Creates a folder named "bar"
        // $this->ftp->mkdir('/img/test2/', DIR_WRITE_MODE);

        // $copyPathDir = '/home/danharoo/www/data/files/goods/goodscode/img/jk3601k1a/';

        // $this->ftp->mirror($copyPathDir, '/img/test2/');

        // $list = $this->ftp->list_files('/'.$clickDir);

        // // debug($list);

        // $dir = '/';
        // if($clickDir1) $dir .= $clickDir1;
        // if($clickDir2) $dir .= '/'.$clickDir2;

        // $list = $this->ftp->list_files($dir);

        // // debug($list);

        // echo 'FTP 루트 목록<br />';

        // foreach ($list as $key => $value)
        // {
        //     if($clickDir1)
        //         echo $value.'<br />';
        //     else
        //         echo '<a href="/products/test'.$value.'">'.$value.'</a><br />';
        // }

        // $this->ftp->close();

		// exit;
		/*
		// 등록 상품 정보 확인
		$sql = "SELECT
					GS.*,
					GSD.Description, GSD.NoticeItemCodes,
					GSI.img0, GSI.img1, GSI.img2, GSI.img3, GSI.img4, GSI.img5, GSI.img6, GSI.img7, GSI.img8, GSI.img9, GSI.img10, GSI.img11, GSI.img12, GSI.img13, GSI.img14
					FROM
						goods AS		GS LEFT OUTER JOIN
						goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
						goods_image		As GSI ON GS.id = GSI.goods_id
					WHERE GS.id='1'
		";
		$query = $this->db->query($sql);
		$row = $query->row();
		debug($row);
		*/

		// $arr = array('a'=>1, 'b'=>2);
		// $object = (object) $arr;
		// //$arr = array('a','b','c');
		// //debug($object);
		// //echo "<BR><BR>";
		// //$d = shuffle($arr);
		// //var_dump($arr);

		// $infos_json = '{"result":"fail","resultMsg":"\ub4f1\ub85d\ud560 \uc218 \uc5c6\ub294 \uc0c1\ud488\uba85\uc785\ub2c8\ub2e4. \uc0c1\ud45c\ubc95, \uc800\uc791\uad8c, \ud37c\ube14\ub9ac\uc2dc\ud2f0\uad8c \ub4f1\uc758 \uce68\ud574 \uc18c\uc9c0\uac00 \uc788\ub294 \uc0c1\ud488\uc744 \ub4f1\ub85d\ud560 \uc218 \uc5c6\uc2b5\ub2c8\ub2e4."}';
		// debug($infos_json);

		// $resultArr = json_decode($infos_json, 1);
		// debug($resultArr);

		// $resultJson = json_encode($resultArr);
		// debug($resultJson);

		// $infos = iconv('UTF-8', 'EUC-KR', $infos_json);
		// debug($infos);

		/*
		$this->load->helper('html');
		$this->view_data['link_tag1'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap.css');
		$this->view_data['link_tag2'] = link_tag('/include/jqgrid/ui.jqgrid-bootstrap-ui.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/extensions/Scroller/css/dataTables.scroller.css');
		//$this->view_data['link_tag3'] = link_tag('/include/DataTables-1.10.7/examples/resources/syntax/shCore.css');

		$this->load->view('top', $this->view_data);
		$this->load->view('products/test', $this->view_data);
		$this->load->view('bottom');
		*/
	}

	// =================================================================
	// Skin8 자동 분할 기능
	// 추가일: 2026-02-03 16:10:51
	// 작성자: Nuri (서개발)
	// =================================================================

	/**
	 * Skin8 이미지 자동 분할 및 저장
	 * 
	 * @author Nuri (서개발)
	 * @date 2026-02-02
	 * @url /goods/detail_save?goodsId=XXX&de_skin=8
	 * 
	 * 기능:
	 * - 모델 사진을 자동으로 03~12번 이미지로 분할
	 * - ImageSplitter 라이브러리 사용
	 * - goods_desc 필드에 HTML 저장
	 */
	public function detail_save()
	{
		error_log('AADS_DEBUG: detail_save called, de_skin=' . $this->input->get('de_skin') . ', goodsId=' . $this->input->get('goodsId'));
		// 파라미터 받기
		$goodsId = $this->input->get('goodsId');
		$de_skin = $this->input->get('de_skin');
		$check = $this->input->get('check');
		
		// goodsId 검증
		if (!$goodsId || $goodsId < 1) {
			echo '<div style="padding:50px;text-align:center;">';
			echo '<h2 style="color:red;">⚠️ 오류</h2>';
			echo '<p>상품 ID가 올바르지 않습니다!</p>';
			echo '<p style="color:#666;">goodsId: ' . htmlspecialchars($goodsId) . '</p>';
			echo '</div>';
			return;
		}
		
		// 상품 정보 조회
		$query = $this->db->query("SELECT * FROM goods WHERE id = " . (int)$goodsId);
		$goods = $query->row_array();
		
		if (!$goods) {
			echo '<div style="padding:50px;text-align:center;">';
			echo '<h2 style="color:red;">⚠️ 오류</h2>';
			echo '<p>상품을 찾을 수 없습니다!</p>';
			echo '<p style="color:#666;">goodsId: ' . htmlspecialchars($goodsId) . '</p>';
			echo '</div>';
			return;
		}
		
		$goodsCode = $goods['GoodsCode'];
		
		// Skin8 자동 분할 처리 (인트로+모델+제품 3종 통합 - 2026-03-20)
		log_message('debug', 'detail_save: goodsId=' . $goodsId . ', de_skin=' . $de_skin . ', goodsCode=' . $goodsCode);
		if ($de_skin == '8') {
			// DB에서 3종 정렬 데이터 직접 조회
			$detailQuery = $this->db->query("SELECT GoodsSortImg1, GoodsSortImg2, GoodsSortImg3 FROM goods_detail WHERE goods_id = " . (int)$goodsId);
			$detailRow = $detailQuery->row();

			// 인트로 이미지 파싱
			$introImages = array();
			if ($detailRow && !empty($detailRow->GoodsSortImg1)) {
				$introImages = array_values(array_filter(explode('||', $detailRow->GoodsSortImg1)));
			}
			// 모델사진 파싱
			$modelImages = array();
			if ($detailRow && !empty($detailRow->GoodsSortImg2)) {
				$modelImages = array_values(array_filter(explode('||', $detailRow->GoodsSortImg2)));
			}
			// 제품사진 파싱
			$productImages = array();
			if ($detailRow && !empty($detailRow->GoodsSortImg3)) {
				$productImages = array_values(array_filter(explode('||', $detailRow->GoodsSortImg3)));
			}
			
			// 3종 중 하나라도 있으면 처리
			if (!empty($introImages) || !empty($modelImages) || !empty($productImages)) {
				// ImageSplitter 라이브러리 로드
				$this->load->library('ImageSplitter', array('goodsCode' => $goodsCode));
				
				// 모델사진 분할 실행
				$modelFiles = array();
				if (!empty($modelImages)) {
					$result = $this->imagesplitter->splitModelImages($modelImages);
					if ($result['success']) {
						$modelFiles = $result['files'];
					}
				}
				
				// 3종 통합 HTML 생성
				$html = $this->imagesplitter->generateFullSkin8Html($introImages, $modelFiles, $productImages);
				
				if (!empty($html)) {
					
					// DB 업데이트 (컬럼명 수정 - 2026-03-20 fix)
					$this->db->where('goods_id', $goodsId);
					$this->db->update('goods_detail', array(
						'Description' => $html,
						'modified' => date('Y-m-d H:i:s')
					));
					// goods 테이블 스킨 업데이트
					$this->db->where('id', $goodsId);
					$this->db->update('goods', array(
						'DeSkin' => 8
					));
					
					// DB 기반으로 변경하여 세션 정리 불필요 (2026-03-20)
					// $this->session->unset_userdata($sessionKey);
					
					// 성공 메시지
					echo '<!DOCTYPE html>';
					echo '<html><head><meta charset="UTF-8">';
					echo '<title>Skin8 이미지 분할 완료</title>';
					echo '<style>';
					echo 'body { font-family: "맑은 고딕", sans-serif; padding: 30px; background: #f5f5f5; }';
					echo '.success-box { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 600px; margin: 0 auto; }';
					echo 'h2 { color: #28a745; margin-bottom: 20px; }';
					echo '.info { background: #e7f3ff; padding: 15px; border-radius: 5px; margin: 15px 0; }';
					echo 'ul { list-style: none; padding: 0; }';
					echo 'li { padding: 5px 0; color: #555; }';
					echo 'li::before { content: "✓ "; color: #28a745; font-weight: bold; }';
					echo '</style></head><body>';
					echo '<div class="success-box">';
					echo '<h2>✅ Skin8 이미지 분할 완료!</h2>';
					echo '<div class="info">';
					echo '<p><strong>상품 코드:</strong> ' . htmlspecialchars($goodsCode) . '</p>';
					echo '<p><strong>인트로:</strong> ' . count($introImages) . '개 | <strong>모델:</strong> ' . count($modelFiles) . '개 | <strong>제품:</strong> ' . count($productImages) . '개</p>';
					echo '</div>';
					echo '<ul>';
					
					if (!empty($modelFiles)) {
						foreach ($modelFiles as $file) {
							echo '<li>' . htmlspecialchars($file['filename']) . '</li>';
						}
					}
					
					echo '</ul>';
					echo '<p style="margin-top:20px;color:#666;font-size:14px;">';
					echo '2초 후 자동으로 새로고침됩니다...';
					echo '</p>';
					echo '</div>';
					echo '<script>setTimeout(function(){ window.location.reload(); }, 2000);</script>';
					echo '</body></html>';
				} else {
					// HTML 생성 실패 (데이터는 있으나 HTML 비어있음)
					echo '<div style="padding:50px;text-align:center;">';
					echo '<h2 style="color:red;">❌ 이미지 처리 실패</h2>';
					echo '<p>이미지 HTML 생성에 실패했습니다.</p>';
					echo '<p style="margin-top:20px;"><button onclick="window.location.reload()">다시 시도</button></p>';
					echo '</div>';
				}
			} else {
				// 인트로/모델/제품 정렬 데이터 모두 없음
				log_message('debug', 'Skin8: DB에 정렬 데이터 없음 (인트로/모델/제품 모두 비어있음) - goodsId: ' . $goodsId);
				
				// 기본 상세 페이지 표시 (기존 로직)
				$this->_display_detail_page($goodsId, $goodsCode, $de_skin, $check);
			}
		} else {
			// Skin8이 아닌 경우 기본 상세 페이지 표시
			$this->_display_detail_page($goodsId, $goodsCode, $de_skin, $check);
		}
	}
	
	/**
	 * 상세 페이지 표시 (기존 로직)
	 * 
	 * @param int $goodsId
	 * @param string $goodsCode
	 * @param int $de_skin
	 * @param bool $check
	 */
	private function _display_detail_page($goodsId, $goodsCode, $de_skin, $check)
	{
		// 기본 상세 페이지 표시
		echo '<!DOCTYPE html>';
		echo '<html><head><meta charset="UTF-8">';
		echo '<title>상품 상세</title></head><body>';
		echo '<div style="padding:30px;">';
		echo '<h3>상품 코드: ' . htmlspecialchars($goodsCode) . '</h3>';
		echo '<p>선택한 스킨: ' . htmlspecialchars($de_skin) . '</p>';
		if ($check) {
			echo '<p>미리보기 모드</p>';
		}
		echo '</div>';
		echo '</body></html>';
	}

}

/* End of file main.php */
/* Location: ./application/controllers/mian.php */
